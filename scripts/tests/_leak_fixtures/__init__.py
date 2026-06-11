# Leading-underscore package so `python3 -m unittest discover -s scripts/tests`
# (pattern test*.py) never collects leak_probe.py, while the dotted form
# `python3 -m unittest scripts.tests._leak_fixtures.leak_probe` can still import
# it. See scripts/tests/test_deliberate_leak_is_caught.py for why this fixture
# is deliberately OUTSIDE the discovered tree (it MUST fail by design).
