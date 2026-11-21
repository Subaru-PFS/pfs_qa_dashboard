"""The PFS QA dashboard: the run report.

drp_qa's `pfs.drp.qa.qadb.reader` reads a collection's QA results from qadb;
`live` keeps them up to date as visits are loaded, `qadbSource` writes and reads
snapshots of a read, and `runReport` judges the visits and makes the page. None
imports the stack or a Butler.
"""
