"""Registry of EPHEMERAL PROBE/FIXTURE MARKERS written into the production class.

Anything that writes a short-lived object into ``ISMA_Quantum`` MUST register its
marker here. This is not documentation: ``validate_production.py`` builds its
residue detectors from it, and ``check_marker_registry.sh`` FAILS when a production
source file contains a marker literal that is not listed.

That gate is the point. ``/__canary__/`` was absent from a hand-maintained detector
list for as long as the list existed and was only found in review. A registry nobody
is forced to update is the same hand-maintained list with extra steps.

WHY THIS IS A .py AND NOT A .tsv. It was a ``.tsv`` first, and the repository's
stay-clean gate correctly rejected it: ``.tsv`` is classified data-shaped, and
data-shaped paths must be deliberately allowlisted. The allowlist was empty, so
adding this file would have been its first entry -- a gate-weakening change,
authored by the person it unblocks, verified by the green it produces. The honest
fix is not an exemption but an accurate representation: a registry that only code
reads is code, not a dataset. No allowlist needed and the gate stays untouched.

The real fix remains moving these writers out of the production class entirely --
a probe/test class deletes the reason this file exists.
"""

# (property, exact marker, token prefilter, writer)
MARKERS = [
    ("source_file", "/__canary__/",                 "*canary*",                    "isma/scripts/disk_headroom_canary.sh"),
    ("source_file", "/__validate__/",               "*validate*",                  "isma/scripts/validate_production.py"),
    ("source_file", "/__fixture__/",                "*fixture*",                   "isma/scripts/verify_authority_filter.py"),
    ("authority",   "__authority_filter_fixture__", "*authority_filter_fixture*",  "isma/scripts/verify_authority_filter.py"),
    # The residue check's own liveness sentinels. Registered for two reasons: the CI
    # gate requires it, and a sentinel abandoned by a SIGKILLed validation run is
    # itself residue and must be detectable. The gate caught this one within minutes
    # of the sentinel code being written, which is the property working on its author.
    ("source_file", "/__sentinel__/",               "*sentinel*",                  "isma/scripts/validate_production.py"),
]
