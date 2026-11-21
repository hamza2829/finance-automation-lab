// Staging query: typed approvals table (load disabled in Excel).
let
    Source = Csv.Document(
        File.Contents(ExportFolder & "approvals.csv"),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Typed = Table.TransformColumnTypes(
        Promoted,
        {
            {"id", Int64.Type},
            {"invoice_id", Int64.Type},
            {"level", Int64.Type},
            {"approver", type text},
            {"status", type text},
            {"comment", type text},
            {"requested_at", type datetime},
            {"decided_at", type datetime}
        },
        "en-US"
    )
in
    Typed
