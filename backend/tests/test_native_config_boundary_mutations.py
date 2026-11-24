import pytest
from round_robin.visual.renderers.interface import (
    IsolatedRendererRequired,
    NativeRendererConfiguration,
)


@pytest.mark.parametrize(
    "image,browser,host",
    [
        ("registry.example/team/app:Release", 250, 1),
        ("local-image", 1000, 1),
        ("local-image", 20000, 20),
    ],
)
def test_valid_image_and_inclusive_deadline_boundaries_are_accepted(
    image, browser, host
):
    config = NativeRendererConfiguration(
        True, image, browser_timeout_ms=browser, host_timeout_seconds=host
    )
    assert config.validate() is None


def test_host_deadline_one_millisecond_shorter_than_browser_is_rejected():
    config = NativeRendererConfiguration(
        True, "local-image", browser_timeout_ms=1001, host_timeout_seconds=1
    )
    with pytest.raises(IsolatedRendererRequired):
        config.validate()
