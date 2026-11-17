"""Cloud fingerprinting (issue #59, ADR 0044): the spool, the job service and the relay.

`spool.py` owns the bytes and their deletion; `service.py` runs one job and registers the result
through the browser upload's own function. `relay.py` is the public host's streaming forwarder
(§9), and `attestation.py` is development's side of the same question — what it checks before it
believes who the relay says is acting — with the counter it writes in `models.py`. The customer API
lives in `marceline/app/uploads.py` and the loop in `manage.py fingerprint_worker`.
"""
