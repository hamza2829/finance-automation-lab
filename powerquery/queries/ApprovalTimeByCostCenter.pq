let
    Grouped = Table.Group(
        ApprovalTime,
        {"cost_center_id"},
        {
            {"approved_count", each Table.RowCount(_), Int64.Type},
            {"avg_hours", each Number.Round(List.Sum([approval_seconds]) / Table.RowCount(_) / 3600, 2), type number}
        }
    ),
    Sorted = Table.Sort(Grouped, {{"cost_center_id", Order.Ascending}})
in
    Sorted
