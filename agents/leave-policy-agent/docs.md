## Leave policy assistant

Answers employee questions about **annual, sick and parental leave**, based on the company leave policy.
It is a Microsoft Foundry prompt agent published through the AI gateway.

**Owner:** HR platform team · **Status:** test

### When to use it

- Employee self-service channels (intranet, chat, mobile) that need answers about leave entitlement and rules.
- Not for submitting leave requests or for other HR topics; the agent tells users to contact HR for those.

### Getting access

1. Sign in to this Developer Portal and subscribe to the **Agents (test)** product.
2. Use the subscription key in the `Ocp-Apim-Subscription-Key` header.

### Calling the agent

`POST /agents/leave-policy-agent/responses`

Send the user's question as `input`. The gateway adds the agent reference; you do not choose a model or agent.

```json
{ "input": "How many days of annual leave do I get each year?" }
```

For a multi-turn conversation, pass the same `conversation` ID on each call.

The response follows the Responses API format. The answer is the `text` of the `message` item in `output`:

```json
{
  "id": "resp_...",
  "status": "completed",
  "agent_reference": { "type": "agent_reference", "name": "leave-policy-agent", "version": "1" },
  "output": [
    { "type": "message", "role": "assistant",
      "content": [ { "type": "output_text", "text": "You get 18 working days of annual leave per calendar year..." } ] }
  ]
}
```

### Limits and errors

| Status | Meaning | What to do |
|---|---|---|
| 401 | Missing or invalid subscription key | Check the `Ocp-Apim-Subscription-Key` header |
| 429 | More than 60 calls a minute or 5,000 a day for your subscription | Wait for the `Retry-After` seconds |
| 503 `AI_ROUTE_SUSPENDED` | The agent is suspended by the platform kill switch | Retry later or contact the platform team |

Every response carries an `x-correlation-id` header; quote it when reporting a problem.

### Data handling

Do not send personal or medical details beyond what the question needs. Calls are logged by the AI
gateway and traced in Foundry for monitoring.
