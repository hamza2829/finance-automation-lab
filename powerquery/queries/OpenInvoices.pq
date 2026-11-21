// Open invoices at the end of the report month.
// open = status received | needs_review | pending_approval | escalated
//        and received before the first day of the next month.
let
    MonthStart = #datetime(Number.From(Text.Start(ReportMonth, 4)), Number.From(Text.End(ReportMonth, 2)), 1, 0, 0, 0),
    MonthEnd = Date.AddMonths(MonthStart, 1),
    OpenStatuses = {"received", "needs_review", "pending_approval", "escalated"},
    Filtered = Table.SelectRows(Invoices, each List.Contains(OpenStatuses, [status]) and [received_at] < MonthEnd),
    Unassigned = Table.ReplaceValue(Filtered, null, "(unassigned)", Replacer.ReplaceValue, {"cost_center_id"}),
    WithAge = Table.AddColumn(Unassigned, "age_days", each Number.RoundDown(Duration.TotalDays(MonthEnd - [received_at])), Int64.Type),
    Selected = Table.SelectColumns(WithAge, {"id", "invoice_number", "vendor_id", "cost_center_id", "status", "gross_cents", "received_at", "age_days"}),
    Sorted = Table.Sort(Selected, {{"received_at", Order.Ascending}, {"id", Order.Ascending}})
in
    Sorted
