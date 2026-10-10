def test_packages_importable() -> None:
    import _shared
    import holiday_calendar
    import security_master
    import ssi_store
    import trade_store

    assert all((_shared, holiday_calendar, security_master, ssi_store, trade_store))
