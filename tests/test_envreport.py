from envreport import EnvReport, main


def test_main(capsys):
    """Test that one full run works"""
    main()
    cap = capsys.readouterr()
    assert cap.out


def test_dict_roundtrip():
    report = EnvReport()
    report.collect()
    report_text = report.text_report()
    report_dict = report.to_dict()
    report2 = EnvReport.from_dict(report_dict)
    report_text_2 = report2.text_report()
    assert report_text == report_text_2
