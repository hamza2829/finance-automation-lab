let
    Grouped = Table.Group(
        OpenInvoices,
        {"status"},
        {
            {"count", each Table.RowCount(_), Int64.Type},
            {"gross_cents", each List.Sum([gross_cents]), Int64.Type}
        }
    ),
    Sorted = Table.Sort(Grouped, {{"status", Order.Ascending}})
in
    Sorted
