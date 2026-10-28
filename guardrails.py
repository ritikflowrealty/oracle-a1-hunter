"""One-off: cap the tenancy to Always Free compute/storage and add a $0.50 budget alert.

Safe to re-run: existing quota policy / budget with the same name are updated, not duplicated.
"""
import os

import oci

from hunter import config

QUOTA_NAME = "always-free-only"
QUOTA_STATEMENTS = [
    "zero compute-core quotas in tenancy",
    "set compute-core quota standard-a1-core-count to 2 in tenancy",
    "set compute-core quota standard-a1-core-regional-count to 2 in tenancy",  # A1 launches check this one
    "zero compute-memory quotas in tenancy",
    "set compute-memory quota standard-a1-memory-count to 12 in tenancy",
    "set compute-memory quota standard-a1-memory-regional-count to 12 in tenancy",
    "set block-storage quota total-storage-gb to 200 in tenancy",
]
BUDGET_NAME = "stay-free"
BUDGET_USD = 0.5
ALERT_EMAIL = os.environ["ALERT_EMAIL"]


def apply_quota(cfg, tenancy):
    q = oci.limits.QuotasClient(cfg)
    existing = [x for x in q.list_quotas(tenancy).data if x.name == QUOTA_NAME]
    if existing:
        q.update_quota(existing[0].id, oci.limits.models.UpdateQuotaDetails(
            statements=QUOTA_STATEMENTS, description="Keep usage inside Always Free"))
        print(f"Updated quota policy {QUOTA_NAME}")
    else:
        q.create_quota(oci.limits.models.CreateQuotaDetails(
            compartment_id=tenancy, name=QUOTA_NAME, statements=QUOTA_STATEMENTS,
            description="Keep usage inside Always Free"))
        print(f"Created quota policy {QUOTA_NAME}")
    for s in QUOTA_STATEMENTS:
        print("  ", s)


def apply_budget(cfg, tenancy):
    b = oci.budget.BudgetClient(cfg)
    existing = [x for x in b.list_budgets(tenancy, target_type="ALL").data if x.display_name == BUDGET_NAME]
    if existing:
        budget = b.update_budget(existing[0].id, oci.budget.models.UpdateBudgetDetails(amount=BUDGET_USD)).data
        print(f"Updated budget {BUDGET_NAME}: ${BUDGET_USD}/month")
    else:
        budget = b.create_budget(oci.budget.models.CreateBudgetDetails(
            compartment_id=tenancy, display_name=BUDGET_NAME, amount=BUDGET_USD, reset_period="MONTHLY",
            target_type="COMPARTMENT", targets=[tenancy],
            description="Alert if anything starts costing money")).data
        print(f"Created budget {BUDGET_NAME}: ${BUDGET_USD}/month")
    if not b.list_alert_rules(budget.id).data:
        b.create_alert_rule(budget.id, oci.budget.models.CreateAlertRuleDetails(
            display_name="any-actual-spend", type="ACTUAL", threshold=100, threshold_type="PERCENTAGE",
            recipients=ALERT_EMAIL,
            message="Oracle spend passed $0.50 this month - something outside Always Free is running."))
        print(f"Created alert: email {ALERT_EMAIL} when actual spend passes ${BUDGET_USD}")
    else:
        print("Alert rule already exists")


if __name__ == "__main__":
    cfg = config()
    apply_quota(cfg, cfg["tenancy"])
    apply_budget(cfg, cfg["tenancy"])
