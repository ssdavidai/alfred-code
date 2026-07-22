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

PRODUCT_STAGES = {
    "backlog",
    "inbox",
    "sprint_queue",
    "active",
    "legacy_active",
    "needs_split",
    "done",
}

SPRINT_STATES = {"active", "closed"}
SPRINT_ITEM_STATES = {
    "active",
    "done",
    "blocked",
    "rejected",
    "needs_split",
}

FIBONACCI_POINTS = {1, 2, 3, 5, 8, 13, 21}

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
    "superseded",
    "closed",
    "blocked",
    "quarantined",
}

TERMINAL_JOB_STATES = {"merged", "superseded", "closed", "quarantined"}
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

PRODUCT_STATUS = {
    "backlog": "Backlog",
    "inbox": "Inbox",
    "sprint_queue": "Sprint queue",
    "needs_split": "Needs splitting",
    "done": "Done",
}
