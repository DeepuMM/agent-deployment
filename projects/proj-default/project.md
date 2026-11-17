## Default project (test)

Agents of the **proj-default** project, published on the AI gateway. They run in their own Microsoft Foundry
project, **proj-default**, and use platform models through the gateway. Each agent is an
operation of this API; see the operation list for what each agent does.

**Owner:** AI platform team · **Status:** test

### Getting access

1. Get a subscription to the **agents-test** product (subscribe in this Developer Portal, or ask the AI
   platform team).
2. One subscription key gives access to **every agent in this project**.
3. Send the key in the `Ocp-Apim-Subscription-Key` header.

### Calling an agent

`POST /ai/proj-default/{agent}/responses` with the user's question:

```json
{ "input": "Your question here" }
```

The gateway adds the agent reference and signs in to Foundry; you do not choose a model or agent version.
For a multi-turn conversation, pass the same `conversation` ID on each call. The answer is the `text` of the
`message` item in `output` (Responses API format).

### Limits

| Limit | Value |
|---|---|
| Project, per subscription | 120 calls a minute, 10,000 a day across all agents |
| Agent, per subscription | Set per agent; see the operation description |

### Errors

| Status | Meaning |
|---|---|
| 401 | Missing or invalid subscription key |
| 429 | A project or agent limit was exceeded; wait for the `Retry-After` seconds |
| 503 `AI_ROUTE_SUSPENDED` | The agent is suspended by the platform kill switch |

Every response carries an `x-correlation-id` header; quote it when reporting a problem.
