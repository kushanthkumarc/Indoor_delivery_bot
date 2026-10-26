# Delivery lifecycle

Delivery creation and history use the existing status values. Dispatch is
ADMIN-only; pickup confirmation is limited to the requester or ADMIN;
cancellation is ADMIN-only. No navigation transport is configured in the
running application. Dispatch, pickup confirmation (which requires return
navigation), and cancellation return HTTP 503 `NAVIGATION_UNAVAILABLE` before
changing state. State-machine transition records remain available to isolated
tests and are not evidence of physical execution.

| Current state | Request/result | Outcome |
|---|---|---|
| `IDLE` | Dispatch requested with no adapter | Remains `IDLE`; HTTP 503 |
| `GO_TO_DESTINATION` | Result supplied to an explicitly injected adapter | `ARRIVED` or failure state |
| `ARRIVED` | Pickup requested with no adapter | Remains `ARRIVED`; HTTP 503 |
| `RETURN_HOME` | Result supplied to an explicitly injected adapter | `HOME` then `IDLE`, or failure state |

No adapter is enabled by default. A future navigation integration must be
reviewed and validated separately before execution routes are enabled. Backend
state changes must not be described as verified motor actuation or physical
stops.
