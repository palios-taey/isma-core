"""
Qwen3-Embedding-8B Server for DGX Spark
Optimized for GB10 Blackwell (SM 12.1)
"""
import os
import time
import torch
import uvicorn
import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer
from typing import List, Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
MODEL_NAME = os.environ.get("MODEL_NAME", "Qwen/Qwen3-Embedding-8B")
MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "256"))
MAX_SEQ_LEN = int(os.environ.get("MAX_SEQ_LEN", "4096"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
USE_COMPILE = os.environ.get("USE_COMPILE", "true").lower() == "true"

app = FastAPI(title="Qwen3-Embedding Server", version="1.0.0")

# Global model and tokenizer
model = None
tokenizer = None
device = None

# Concurrency control - limit to 4 concurrent inferences per instance
# Prevents GPU memory fragmentation when multiple requests arrive simultaneously
inference_semaphore = asyncio.Semaphore(4)

class EmbeddingRequest(BaseModel):
    texts: List[str]
    batch_size: Optional[int] = 64

class EmbeddingResponse(BaseModel):
    embeddings: List[List[float]]
    dimensions: int
    tokens_processed: int
    latency_ms: float

class HealthResponse(BaseModel):
    status: str
    model: str
    device: str
    memory_used_gb: float
    memory_total_gb: float

@app.on_event("startup")
async def load_model():
    global model, tokenizer, device

    logger.info(f"Loading model: {MODEL_NAME}")
    start = time.time()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Using device: {device}")

    if device.type == "cuda":
        logger.info(f"GPU: {torch.cuda.get_device_name(0)}")
        logger.info(f"Compute Capability: {torch.cuda.get_device_capability(0)}")

    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
    model = AutoModel.from_pretrained(
        MODEL_NAME,
        trust_remote_code=True,
        torch_dtype=torch.bfloat16
    )
    model = model.to(device)
    model.eval()

    if USE_COMPILE and device.type == "cuda":
        logger.info("Compiling model with torch.compile()...")
        model = torch.compile(model, mode="reduce-overhead")
        # Warmup compile
        with torch.no_grad():
            warmup_inputs = tokenizer(["warmup"], return_tensors="pt", padding=True).to(device)
            _ = model(**warmup_inputs)
        logger.info("Model compiled and warmed up")

    load_time = time.time() - start
    logger.info(f"Model loaded in {load_time:.1f}s")

    if device.type == "cuda":
        mem_used = torch.cuda.memory_allocated() / 1e9
        logger.info(f"GPU Memory used: {mem_used:.1f} GB")

@app.get("/health", response_model=HealthResponse)
async def health():
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    mem_used = 0
    mem_total = 0
    if device.type == "cuda":
        mem_used = torch.cuda.memory_allocated() / 1e9
        mem_total = torch.cuda.get_device_properties(0).total_memory / 1e9

    return HealthResponse(
        status="healthy",
        model=MODEL_NAME,
        device=str(device),
        memory_used_gb=round(mem_used, 2),
        memory_total_gb=round(mem_total, 2)
    )

@app.post("/embed", response_model=EmbeddingResponse)
async def embed(request: EmbeddingRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if len(request.texts) == 0:
        raise HTTPException(status_code=400, detail="No texts provided")

    if len(request.texts) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size {len(request.texts)} exceeds maximum {MAX_BATCH_SIZE}"
        )

    # Pre-validation: Estimate token counts to fail fast on oversized inputs
    # (Proper phi-tiling ensures chunks are < MAX_SEQ_LEN, so this should never trigger)
    CHARS_PER_TOKEN = 4  # Conservative estimate
    for idx, text in enumerate(request.texts):
        estimated_tokens = len(text) // CHARS_PER_TOKEN
        if estimated_tokens > MAX_SEQ_LEN * 1.2:  # 20% buffer for safety
            raise HTTPException(
                status_code=400,
                detail=f"Text at index {idx} appears too long (~{estimated_tokens} tokens). "
                       f"Maximum is {MAX_SEQ_LEN}. Use phi-tiling to chunk text before embedding."
            )

    # Acquire semaphore - only one inference at a time to prevent GPU memory fragmentation
    async with inference_semaphore:
        start = time.time()
        total_tokens = 0
        all_embeddings = []

        # Process in batches
        batch_size = min(request.batch_size, len(request.texts))

        try:
            with torch.no_grad():
                for i in range(0, len(request.texts), batch_size):
                    batch_texts = request.texts[i:i+batch_size]

                    # NO TRUNCATION - phi-tiling must ensure texts fit
                    # If this errors, caller sent un-tiled text
                    inputs = tokenizer(
                        batch_texts,
                        padding=True,
                        truncation=False,  # FAIL LOUDLY - no silent truncation
                        max_length=MAX_SEQ_LEN,
                        return_tensors="pt"
                    ).to(device)

                    total_tokens += inputs["input_ids"].numel()

                    outputs = model(**inputs)

                    # Mean pooling — matches the pooling used when the 1M+ tile
                    # index was built.  Qwen3-Embedding-8B natively uses last-token
                    # pooling, but switching query-side pooling without re-embedding
                    # the corpus creates a vector-space mismatch (0.916 cosine sim
                    # between mean and last-token for same text).  Keep mean pooling
                    # until a full corpus re-embed is feasible.
                    attention_mask = inputs["attention_mask"].unsqueeze(-1)
                    token_embeddings = outputs.last_hidden_state
                    sum_embeddings = torch.sum(token_embeddings * attention_mask, dim=1)
                    sum_mask = attention_mask.sum(dim=1).clamp(min=1e-9)
                    embeddings = sum_embeddings / sum_mask

                    all_embeddings.append(embeddings.cpu())

                    # Clear intermediate tensors to free VRAM
                    del inputs, outputs, token_embeddings, attention_mask
        finally:
            # Always cleanup GPU memory after inference
            if device.type == "cuda":
                pass  # torch.cuda.empty_cache() removed - defeats caching

        # Concatenate all batches
        final_embeddings = torch.cat(all_embeddings, dim=0)

        latency_ms = (time.time() - start) * 1000

        return EmbeddingResponse(
            embeddings=final_embeddings.tolist(),
            dimensions=final_embeddings.shape[-1],
            tokens_processed=total_tokens,
            latency_ms=round(latency_ms, 2)
        )

@app.post("/v1/embeddings")
async def openai_embeddings(request: dict):
    """OpenAI-compatible /v1/embeddings endpoint.

    Translates between OpenAI format and internal /embed format.
    """
    inp = request.get("input", [])
    if isinstance(inp, str):
        inp = [inp]

    embed_req = EmbeddingRequest(texts=inp)
    result = await embed(embed_req)

    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": emb, "index": i}
            for i, emb in enumerate(result.embeddings)
        ],
        "model": request.get("model", MODEL_NAME),
        "usage": {
            "prompt_tokens": result.tokens_processed,
            "total_tokens": result.tokens_processed,
        },
    }


@app.get("/v1/models")
async def openai_models():
    """OpenAI-compatible /v1/models endpoint."""
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "local"}],
    }


@app.get("/")
async def root():
    return {
        "service": "Qwen3-Embedding Server",
        "model": MODEL_NAME,
        "endpoints": ["/health", "/embed", "/v1/embeddings"],
        "max_batch_size": MAX_BATCH_SIZE,
        "max_seq_len": MAX_SEQ_LEN
    }

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, workers=1)
