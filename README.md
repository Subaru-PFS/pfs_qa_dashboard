# pfs_qa_dashboard

A browser dashboard of the PFS image QA gate: every image's verdict and the metrics behind it, live during a run
from qadb (with the opdb's IIC sequences, visit times and observers' notes), or offline from a snapshot of a qadb
read. It reads what drp_qa's QA tasks stored and judged; it computes no metric of its own.

Built on Panel, HoloViews, Datashader and DuckDB, and on drp_qa (`pfs.drp.qa`) for reading qadb and for the
thresholds the tasks judge with.
