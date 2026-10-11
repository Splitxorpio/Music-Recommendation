"""Frequency's multi-user backend: PostgreSQL-backed catalog, jobs, quota scheduling, accounts and API.

The original single-user scripts at the repository root keep working unchanged; this package reuses their
algorithms (taste_profile, recommend, calibrate) and their provider knowledge (net.SERVICES limits).
"""
