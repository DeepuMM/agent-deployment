Answers employee questions about **travel and expense claims**: deadlines, receipts, hotel and meal limits,
and what is not reimbursed. It is a Microsoft Foundry prompt agent in the **proj-default** project.

**Owner:** finance platform team · **Status:** test

#### Calling the agent

`POST /ai/proj-default/expense-policy-agent/responses`

```json
{ "input": "What is the hotel limit for domestic travel?" }
```

The answer is the `text` of the `message` item in `output`. Pass the same `conversation` ID on each call for
a multi-turn conversation.

#### Not covered

Submitting claims or checking claim status; the agent tells users to use the expense system for those.
