// Invoices rejected in the report month, with the approver's comment.
let
    MonthStart = #datetime(Number.From(Text.Start(ReportMonth, 4)), Number.From(Text.End(ReportMonth, 2)), 1, 0, 0, 0),
    MonthEnd = Date.AddMonths(MonthStart, 1),
    Rejected = Table.SelectRows(
        Invoices,
        each [status] = "rejected" and [closed_at] <> null and [closed_at] >= MonthStart and [closed_at] < MonthEnd
    ),
    RejectedApprovals = Table.SelectRows(Approvals, each [status] = "rejected"),
    Joined = Table.NestedJoin(Rejected, {"id"}, RejectedApprovals, {"invoice_id"}, "approval", JoinKind.LeftOuter),
    Expanded = Table.ExpandTableColumn(Joined, "approval", {"comment", "approver"}, {"comment", "rejected_by"}),
    Selected = Table.SelectColumns(Expanded, {"id", "invoice_number", "vendor_id", "cost_center_id", "gross_cents", "closed_at", "rejected_by", "comment"}),
    Sorted = Table.Sort(Selected, {{"id", Order.Ascending}})
in
    Sorted
