# Pirate outcome causes

Local producers use the closed shared [Common cause vocabulary](https://github.com/the-boring-crocodile/boring-pond-common/tree/main/packages/boring-outcomes)
plus the component additions below. A cause is independent of status: refused
means nothing started or changed; failed means work started and may have effects.
Valid received outcomes retain their original cause and details.

The declarations and reviewed dynamic exceptions live in
[the source test](tests/test_local_cause_codes.py). It scans src for literal
codes in Refused/Failed calls. A new local cause needs a meaning and must not
be a synonym of a shared cause.

| Extra cause | Meaning |
|---|---|
| pirate_failed | an unexpected local Pirate operation failed; consult its log |
| save_to_failed | the service answered but saving its answer failed or is uncertain |
| save_to_refused | the service answered but saving its answer was refused |

The save wrapper reports that the service call ran even when the subsequent save
was refused. The original service answer and flat save outcome remain in the
result; the wrapper does not imply that the service call had no effects.
SAVE_PROBLEMS in the assignment owner is the reviewed finite selector. Received authority and
service causes remain unchanged.
