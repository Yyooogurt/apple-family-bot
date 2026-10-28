# Validation

On October 28, 2026, the simplified version passed 26 local tests on Python 3.12.14. The tests covered calculations, rate validation, delivery safeguards, HTML escaping and token-safe errors. Both workflows were parsed as YAML.

GitHub Actions and real Telegram delivery were not tested. The NBU request returned HTTP 403 in the preparation environment; use the `preview` action to check live rates on GitHub.
