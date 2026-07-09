# Slack driver — Socket Mode bot for inbound DMs/@mentions + owner-gated
# outbound via chat.postMessage. Two supervised processes (slack-in /
# slack-out), the iMessage two-process layout: a Slack send is a stateless
# HTTP call, not socket-bound, so outbound runs as its own process and an
# owner-approved send is delivered inline by the approvals driver.
