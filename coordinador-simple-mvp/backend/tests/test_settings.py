from app.settings import find_workspace_root


def test_workspace_root_is_discovered_from_local_llm_file(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "local_llm.py").write_text("", encoding="utf-8")

    settings_dir = tmp_path / "Proyecto" / "repo" / "backend" / "app"
    settings_dir.mkdir(parents=True)
    settings_file = settings_dir / "settings.py"
    settings_file.write_text("", encoding="utf-8")

    assert find_workspace_root(settings_file) == tmp_path
