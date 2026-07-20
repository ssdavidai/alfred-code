ISSUE_STATES = {
    "observed",
    "planning",
    "awaiting_approval",
    "approved",
    "building",
    "ready_merge",
    "completed",
    "blocked",
    "closed",
}

JOB_STATES = {
    "queued",
    "waiting_dependency",
    "waiting_lane",
    "launching",
    "running",
    "pr_open",
    "reviewing",
    "repairing",
    "ready_merge",
    "merged",
    "closed",
    "blocked",
    "quarantined",
}

TERMINAL_JOB_STATES = {"merged", "closed", "quarantined"}
ACTIVE_LEASE_STATES = {
    "launching",
    "running",
    "pr_open",
    "reviewing",
    "repairing",
    "ready_merge",
}

PROJECT_STATUS = {
    "observed": "Inbox",
    "planning": "Specifying",
    "awaiting_approval": "Approval",
    "approved": "Queued",
    "building": "Building",
    "ready_merge": "Ready to merge",
    "completed": "Done",
    "blocked": "Blocked",
    "closed": "Done",
}
