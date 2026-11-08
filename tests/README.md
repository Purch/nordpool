# Focused regression tests

Run from the repository root in an isolated virtual environment:

```sh
python -m pip install -r requirements_test.txt
python -m pytest tests -q
python -m black --check custom_components/nordpool/__init__.py custom_components/nordpool/misc.py custom_components/nordpool/sensor.py tests
git diff --check
```

The suite uses lightweight Home Assistant stubs, installed by `conftest.py`.
It exercises the integration callbacks, price cache, API-client boundary and
sensor-facing values without a running Home Assistant instance or network calls.
It is not a Home Assistant config-entry or storage lifecycle test. Do not load
this conftest into a real Home Assistant test suite; run those tests separately.

Covered scenarios include delayed and partial publication, timeout/transport
failures and recovery across midnight, requests that overlap, 23/24/25-hour
delivery days, and restoration without double-applying VAT or unit conversion.
The normal midnight rollover and complete-cache timers must not fetch prices.
Missing prices retain the last published finite value, including the public
`current_price` attribute. A startup without any known price does not invent one.
