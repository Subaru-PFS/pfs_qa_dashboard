"""The PFS QA dashboard.

drp_qa's `pfs.drp.qa.qadb.reader` reads a collection's QA results from qadb;
`qadbSource` builds them into the metrics table (and writes and reads snapshots
of a read), `serving` holds how the table is built and the thresholds the tasks
judge with, `store` queries it with DuckDB, and `live` keeps it up to date as
visits are loaded, with the opdb's sequences, times and notes. None imports the
stack, Panel or a Butler. `app` is the Panel app over a `store.MetricsStore`.
"""
