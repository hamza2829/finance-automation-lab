let
    Grouped = Table.Group(
        RejectedItems,
        {"cost_center_id"},
        {
            {"count", each Table.RowCount(_), Int64.Type},
            {"gross_cents", each List.Sum([gross_cents]), Int64.Type}
        }
    ),
    Sorted = Table.Sort(Grouped, {{"cost_center_id", Order.Ascending}})
in
    Sorted
