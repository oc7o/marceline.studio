"""Browser frame capture (ADR 0016, enabled by the operator 2026-09-16).

Lives in the project package because one capture needs `crawler` (robots.txt, the block/challenge
and age-gate detector), `matching` (the triage queue, PDQ hashing) and `index` (the page and its
safety state) together — a combination no app may import, exactly like `marceline.scheduler`.
"""
