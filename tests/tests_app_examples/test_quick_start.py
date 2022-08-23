import logging
import os
from unittest import mock

import pytest
from click.testing import CliRunner
from tests_app import _PROJECT_ROOT

from lightning_app import LightningApp
from lightning_app.cli.lightning_cli import run_app
from lightning_app.testing.helpers import RunIf
from lightning_app.testing.testing import application_testing, run_app_in_cloud, wait_for


class QuickStartApp(LightningApp):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.root.serve_work._parallel = True

    def run_once(self):
        done = super().run_once()
        if self.root.train_work.best_model_path:
            return True
        return done


def test_quick_start_example(caplog, monkeypatch):
    """This test ensures the Quick Start example properly train and serve PyTorch Lightning."""

    command_line = [
        os.path.join(_PROJECT_ROOT, "lightning-quick-start/app.py"),
        "--blocking",
        "False",
        "--open-ui",
        "False",
    ]
    result = application_testing(QuickStartApp, command_line)
    assert result.exit_code == 0


@pytest.mark.cloud
def test_quick_start_example_cloud() -> None:
    with run_app_in_cloud(os.path.join(_PROJECT_ROOT, "lightning-quick-start/")) as (_, view_page, _, _):

        def click_gradio_demo(*_, **__):
            button = view_page.locator('button:has-text("Interactive demo")')
            button.wait_for(timeout=3 * 1000)
            button.click()
            return True

        wait_for(view_page, click_gradio_demo)

        def check_examples(*_, **__):
            view_page.reload()
            locator = view_page.frame_locator("iframe").locator('button:has-text("Submit")')
            locator.wait_for(timeout=10 * 1000)
            if len(locator.all_text_contents()) > 0:
                return True

        wait_for(view_page, check_examples)
