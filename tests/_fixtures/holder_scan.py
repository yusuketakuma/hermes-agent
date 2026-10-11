"""Confine the non-Linux state.db holder scan to the test's own process tree."""


def scan_only_own_process_tree(monkeypatch):
    """Make ``psutil.process_iter`` (the macOS/BSD leg of ``foreign_state_db_holders``) see only
    this test process's descendants.

    The host's unrelated processes hold files that the scan, and psutil's own ``open_files``,
    stat. A live Hermes holding files under the real home turns that into a home-I/O guard
    refusal, which the fail-closed scan reports as an unknown holder. The real holders these
    tests spawn are children of the test process, so they stay visible.
    """
    import psutil

    def process_iter(attrs=None, ad_value=None):
        for child in psutil.Process().children(recursive=True):
            try:
                child.info = child.as_dict(attrs, ad_value)
            except psutil.NoSuchProcess:
                continue
            yield child

    monkeypatch.setattr(psutil, "process_iter", process_iter)
