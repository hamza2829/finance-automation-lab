// Staging query: typed invoice table (load disabled in Excel).
let
    Source = Csv.Document(
        File.Contents(ExportFolder & "invoices.csv"),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed = Table.TransformColumnTypes(
        Promoted,
        {
            {"id", Int64.Type},
            {"invoice_number", type text},
            {"vendor_id", type text},
            {"cost_center_id", type text},
            {"booking_category", type text},
            {"status", type text},
            {"gross_cents", Int64.Type},
            {"received_at", type datetime},
            {"closed_at", type datetime}
        },
        "en-US"
    ),
    // empty CSV cells arrive as "" for text columns
    CostCenterNull = Table.ReplaceValue(Typed, "", null, Replacer.ReplaceValue, {"cost_center_id"})
in
    CostCenterNull
