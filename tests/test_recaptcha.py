from unittest.mock import MagicMock

from map_miner.recaptcha_solver import RecaptchaSolver


def test_recaptcha_solver_init():
    mock_page = MagicMock()
    solver = RecaptchaSolver(mock_page, debug=False)
    assert solver.page == mock_page
    assert not solver.debug
    assert solver.current_diag_dir is None


def test_recaptcha_solver_aliases():
    mock_page = MagicMock()
    solver = RecaptchaSolver(mock_page)
    # Check backward compatibility aliases point to the snake_case methods
    assert solver.solveCaptcha == solver.solve_captcha
    assert solver.solveAudioCaptcha == solver.solve_audio_captcha
    assert solver.isSolved == solver.is_solved
