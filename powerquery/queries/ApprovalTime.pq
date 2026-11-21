// Approval time = closed_at - received_at for invoices approved in the report month.
let
    MonthStart = #datetime(Number.From(Text.Start(ReportMonth, 4)), Number.From(Text.End(ReportMonth, 2)), 1, 0, 0, 0),
    MonthEnd = Date.AddMonths(MonthStart, 1),
    Approved = Table.SelectRows(
        Invoices,
        each [status] = "approved" and [closed_at] <> null and [closed_at] >= MonthStart and [closed_at] < MonthEnd
    ),
    WithSeconds = Table.AddColumn(Approved, "approval_seconds", each Duration.TotalSeconds([closed_at] - [received_at]), Int64.Type),
    EscalatedIds = List.Buffer(Table.SelectRows(Approvals, each [level] = 2)[invoice_id]),
    WithEscalation = Table.AddColumn(WithSeconds, "was_escalated", each List.Contains(EscalatedIds, [id]), type logical),
    WithHours = Table.AddColumn(WithEscalation, "approval_hours", each Number.Round([approval_seconds] / 3600, 2), type number),
    Sorted = Table.Sort(WithHours, {{"id", Order.Ascending}})
in
    Sorted
