# commitlayer_x402

CommitLayer for agents that pay over [x402](https://github.com/coinbase/x402). The observer sits on the HTTP
client the agent pays with, watches the three protocol headers, and records what was bought so the x402 Service
Delivery pack can verify it: **did the payment settle once for the quoted amount, and was the service
delivered?** Two questions, two evidence paths, neither answered by the agent.

```bash
pip install 'commitlayer-integrations[x402]'
export COMMITLAYER_RECORDER_KEY=...   # actions:record; never log or print it
```

## What it watches

| Header | On | What the observer reads |
|---|---|---|
| `PAYMENT-REQUIRED` | the `402` response | the quote: `accepts[0]` (`scheme`, `network`, `amount` in atomic units, `asset`, `payTo`) and `resource.url` |
| `PAYMENT-SIGNATURE` | the paid retry | `accepted` (the quote the client chose) and `payload.authorization.nonce`; the signature value itself is never kept |
| `PAYMENT-RESPONSE` | the final response | `success`, `transaction`, `network`, `payer`, `errorReason` |

The v1 spellings (`X-PAYMENT-REQUIRED`, `X-PAYMENT`, `X-PAYMENT-RESPONSE`) are recognised on a best-effort basis.

## Use

```python
import httpx
from commitlayer import Client
from commitlayer_x402 import X402Observer

recorder = Client(api_key=os.environ["COMMITLAYER_RECORDER_KEY"])
observer = X402Observer(recorder, kind="api_call", contract_version=3, decimals=6)
http = httpx.Client(event_hooks=observer.httpx_hooks())

r = http.get("https://api.example-data.com/v1/quote")   # 402 → your x402 client pays → 200
print(observer.last.action_id, observer.last.subject_id, observer.last.settlement, observer.last.delivered)
```

The x402 client library does the paying; the observer only reads the headers it sends and receives. `kind` is
one of `api_call`, `browser_session`, `dataset`, `model_inference`, `compute_resource`, `mcp_tool_call` and selects
the pack contract (`x402_purchase_<kind>`). `decimals` is the asset's (USDC: 6); `None` keeps atomic units. Any
other client is driven through `observer.on_request(url, headers)` and `observer.on_response(url, status, headers,
request_headers=...)`.

What is recorded: the action `purchase_<kind>` on the subject `<nonce>` (the exact scheme's 32-byte nonce, which
the facilitator's settlement and the on-chain transfer repeat), with `amount`, `amount_atomic`, `asset`,
`network`, `pay_to`, `resource`, `provider`, `payer`; then a `settlement` fact (`status` settled, pending for the
facilitator's `settlement_pending`, or failed) and a `delivered` (2xx: `http_status`, `bytes`) or
`delivery_failed` fact.

## Where it runs decides what the facts are worth

Inside the agent's own process the facts travel with the recorder key and the service classes them
`agent_self_report`: shown as what the agent saw, never counted as proof. In a gateway, proxy or tool server that
terminates the 402 flow for the agent, give the observer an evidence client:

```python
evidence = Client(api_key=os.environ["COMMITLAYER_EVIDENCE_KEY"])   # facts:write only
observer = X402Observer(recorder, evidence=evidence, kind="api_call", contract_version=3)
```

Now the same facts are `customer_backend` (shared) and satisfy the pack's contracts. For independent settlement
evidence, a chain watcher pushes `settlement` facts with a `blockchain_confirmation` evidence key and the pack's
`settlement_source` is set to `chain`.

## What it never does

It never pays, signs, retries or blocks a request (fail-open: a CommitLayer error is logged and the purchase
proceeds; `fail_open=False` raises instead). It never stores a body, a `PAYMENT-SIGNATURE` value, an
`Authorization` header, a wallet key, a seed phrase or a facilitator credential. Its only state is the last quote per
URL, in memory.

The paid request leaves after the action is recorded, and the client waits and sends the recording again when
the API answers `429` or `503` with `Retry-After` (see [When the API is busy](../README.md#when-the-api-is-busy)).
A signed payment is valid for a limited time (`maxTimeoutSeconds`), so give the observer a client whose bound is
well inside it, for example `Client(max_retries=1, max_retry_wait=2.0)`, or `max_retries=0` to never wait.

The agent may declare its intent. It may read the verdict. It must never control the independent facts, the
governing contract, or the final evidence.
