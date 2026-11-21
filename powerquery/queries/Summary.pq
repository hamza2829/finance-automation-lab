// One row per KPI. Median is computed explicitly (sort + middle) so the
// definition is identical to powerquery/m_port.py and powerquery/report.py.
let
    Seconds = List.Sort(ApprovalTime[approval_seconds]),
    Count = List.Count(Seconds),
    Median =
        if Count = 0 then null
        else if Number.IsOdd(Count) then Seconds{Number.IntegerDivide(Count, 2)}
        else (Seconds{Count / 2 - 1} + Seconds{Count / 2}) / 2,
    Hours = (s) => if s = null then null else Number.Round(s / 3600, 2),
    Result = #table(
        type table [metric = text, value = any],
        {
            {"report_month", ReportMonth},
            {"open_count", Table.RowCount(OpenInvoices)},
            {"open_gross_cents", List.Sum(OpenInvoices[gross_cents]) ?? 0},
            {"approved_count", Count},
            {"avg_approval_hours", if Count = 0 then null else Hours(List.Sum(Seconds) / Count)},
            {"median_approval_hours", Hours(Median)},
            {"max_approval_hours", Hours(List.Max(Seconds))},
            {"within_3_days_pct", if Count = 0 then null else Number.Round(List.Count(List.Select(Seconds, each _ <= 259200)) / Count * 100, 1)},
            {"escalated_count", List.Count(List.Select(ApprovalTime[was_escalated], each _))},
            {"rejected_count", Table.RowCount(RejectedItems)},
            {"rejected_gross_cents", List.Sum(RejectedItems[gross_cents]) ?? 0}
        }
    )
in
    Result
