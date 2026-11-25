# Refund SLA Engine — Raw Stakeholder PRD

We are getting too many customer complaints about slow card refunds and occasional unauthorized large payouts.
Build a Python service that checks whether a refund can be auto-approved quickly under our 3-day SLA or needs manager sign-off.
Large refunds over $500 or anything flagged as a chargeback shouldn't be paid out automatically.
Also support agents need clear guidance on when to escalate disputes to the risk desk.
