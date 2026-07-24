"""Trains and validates a statistical-learning replacement for
`monitoring/telegram_bot.py`'s naive equal-weight BUY/SELL vote
(`summarize_indicator_signal()`), which `backtester/signal_validation.py`
already measured at ~49.66% out-of-sample directional accuracy — no better
than chance. This module asks the natural follow-up question: does
*learning* how to weight the same four indicators (rather than a
hand-picked -1/0/+1 vote) find any real edge?

Still monitoring-only: nothing here is consulted by `main.py`'s actual
trading decisions (`indicators/math_engine.py`'s module docstring
constraint carries forward unchanged).

`scikit-learn` is a dev/research-only dependency (`pyproject.toml`'s `dev`
extra) — this module is never imported by the live bot. If logistic
regression clears `evaluate_promotion_bar()`'s bar, its fitted
coefficients are hardcoded as plain `float` constants directly in
`monitoring/telegram_bot.py` (see `raw_space_coefficients`/
`raw_space_intercept` below) so the live process never imports `sklearn`
at all — only this offline research module and
`backtester/run_ml_signal_research.py` do.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler

from backtester.signal_validation import (
    DEFAULT_HORIZON_BARS,
    _compute_indicator_arrays,
    build_forward_labels,
    validate_signal,
)
from broker.mt5_gateway import BarSeries
from indicators.math_engine import FloatArray

FEATURE_NAMES: tuple[str, str, str, str] = (
    "ma_distance_pct",
    "rsi",
    "macd_histogram_pct",
    "bollinger_percent_b",
)

DEFAULT_IS_FRACTION = 0.7
MIN_USABLE_SAMPLES = 500

LOGISTIC_REGRESSION_C_GRID: tuple[float, ...] = (0.001, 0.01, 0.1, 1.0, 10.0, 100.0)
LOGISTIC_REGRESSION_MAX_ITER = 1000
TIME_SERIES_CV_SPLITS = 5

GRADIENT_BOOSTING_MAX_DEPTH = 3
GRADIENT_BOOSTING_MAX_ITER = 100

RANDOM_STATE = 42
CONFIDENCE_Z = 1.96  # ~95% two-sided normal-approximation binomial CI

# A practical floor for "discriminates at all" (0.5 = pure chance), not a
# formally derived significance threshold (that would need e.g. DeLong's
# test) — chosen so a model that has been regularized down to effectively
# always predicting the majority class cannot pass just by nudging past
# the majority-class-baseline gate. See evaluate_promotion_bar()'s
# docstring for why this gate exists at all.
MIN_ROC_AUC_FOR_PROMOTION = 0.53


def build_feature_matrix(h1_bars: BarSeries) -> tuple[FloatArray, npt.NDArray[np.bool_]]:
    """`(features [n, 4], valid_mask [n])` — continuous transforms of the
    same 4 indicators `summarize_indicator_signal()` already votes on
    (reusing `signal_validation._compute_indicator_arrays()`), instead of
    that function's -1/0/+1 pre-quantization:

    - `ma_distance_pct`: `(close - ma) / ma` — price's distance from MA,
      normalized so it's comparable across gold's price history.
    - `rsi`: used as-is (already a bounded 0-100 oscillator).
    - `macd_histogram_pct`: `macd_histogram / close` — normalized by price
      for the same reason as `ma_distance_pct`.
    - `bollinger_percent_b`: `(close - lower) / (upper - lower)` — position
      within the bands, 0-1 scale (a degenerate zero-width band, which real
      market data essentially never produces, resolves to `0.5`).

    `valid_mask[i]` is `False` only during each indicator's shared warm-up
    period at the start of the series (`indicators/math_engine.py`'s
    functions left-pad with `NaN`); every model function in this module
    treats warm-up as "not enough history yet", not an error.
    """
    closes = h1_bars.close
    ma, rsi_values, macd_histogram, upper, lower = _compute_indicator_arrays(closes)

    ma_distance_pct = (closes - ma) / ma
    macd_histogram_pct = macd_histogram / closes
    band_width = upper - lower
    percent_b = np.where(
        band_width > 0, (closes - lower) / np.where(band_width > 0, band_width, 1.0), 0.5
    )

    features = np.column_stack([ma_distance_pct, rsi_values, macd_histogram_pct, percent_b])
    valid_mask = ~np.isnan(features).any(axis=1)
    return features, valid_mask


def prepare_training_data(
    h1_bars: BarSeries, *, horizon_bars: int = DEFAULT_HORIZON_BARS
) -> tuple[FloatArray, FloatArray, int]:
    """`(X, y, first_valid_bar_index)`: `X`/`y` are the usable rows only
    (indicator warm-up trimmed, and the trailing `horizon_bars` rows that
    have no forward label yet). `first_valid_bar_index` is `X`'s row 0's
    index into the original `h1_bars` — indicator warm-up is a single
    contiguous prefix (these are rolling/recursive functions over
    gap-free bar data; once warmed up they never re-emit `NaN`), so caller
    code can recover exactly which original bar range `X`/`y` cover, e.g.
    to score `validate_signal()` over the identical out-of-sample range
    for an apples-to-apples comparison (see `train_and_validate_models()`).
    """
    features, valid_mask = build_feature_matrix(h1_bars)
    labels = build_forward_labels(h1_bars.close, horizon_bars=horizon_bars)
    n_usable = len(labels)

    mask = valid_mask[:n_usable]
    first_valid_bar_index = int(np.argmax(mask)) if mask.any() else n_usable
    X = features[:n_usable][mask]
    y = labels[mask]
    return X, y, first_valid_bar_index


@dataclass(frozen=True, slots=True)
class ModelEvaluation:
    """Out-of-sample metrics shared by both candidate models."""

    oos_accuracy: float
    oos_precision: float
    oos_recall: float
    oos_roc_auc: float


@dataclass(frozen=True, slots=True)
class LogisticRegressionEvaluation:
    """`raw_space_coefficients`/`raw_space_intercept` already fold the
    training-time `StandardScaler` into the fit — apply them directly to
    *unscaled* `FEATURE_NAMES` values (`sigmoid(coefficients . x +
    intercept)`), no scaler needed at inference time. This is what makes
    hardcoding them as plain constants in `monitoring/telegram_bot.py`
    possible without shipping a scaler alongside."""

    evaluation: ModelEvaluation
    raw_space_coefficients: tuple[float, ...]
    raw_space_intercept: float
    selected_c: float


@dataclass(frozen=True, slots=True)
class MLValidationReport:
    feature_names: tuple[str, ...]
    horizon_bars: int
    n_train: int
    n_oos: int
    majority_class_baseline: float
    naive_heuristic_oos_accuracy: float
    logistic_regression: LogisticRegressionEvaluation
    gradient_boosting: ModelEvaluation


def _fold_scaler_into_coefficients(
    scaled_coefficients: FloatArray, scaled_intercept: float, means: FloatArray, scales: FloatArray
) -> tuple[tuple[float, ...], float]:
    """`scaled_coefficients`/`scaled_intercept` were fit on
    `(x - means) / scales`; algebraically re-expressing that in terms of
    raw `x` gives `coefficients / scales` and `intercept -
    sum(coefficients * means / scales)` — see `LogisticRegressionEvaluation`'s
    docstring for why this matters."""
    raw_coefficients = scaled_coefficients / scales
    raw_intercept = scaled_intercept - float(np.sum(scaled_coefficients * means / scales))
    return tuple(float(c) for c in raw_coefficients), raw_intercept


def _select_logistic_regression_c(
    X_train: FloatArray,
    y_train: FloatArray,
    *,
    c_grid: tuple[float, ...] = LOGISTIC_REGRESSION_C_GRID,
    n_splits: int = TIME_SERIES_CV_SPLITS,
) -> float:
    """Picks the `C` (inverse regularization strength) that maximizes mean
    validation accuracy across `TimeSeriesSplit` folds — every fold trains
    only on data chronologically before its validation slice, unlike
    ordinary k-fold CV (`docs/adr/ADR-0004-anchored-walk-forward-validation.md`'s
    own rationale against k-fold on price series applies here too, just at
    a lighter weight appropriate for a monitoring feature: this selects
    one hyperparameter, not a strategy's live-trading parameters). The
    held-out OOS split itself is never touched by this search."""
    splitter = TimeSeriesSplit(n_splits=n_splits)
    best_c = c_grid[0]
    best_mean_accuracy = -1.0
    for c in c_grid:
        fold_accuracies: list[float] = []
        for train_idx, val_idx in splitter.split(X_train):
            scaler = StandardScaler().fit(X_train[train_idx])
            model = LogisticRegression(
                C=c, max_iter=LOGISTIC_REGRESSION_MAX_ITER, random_state=RANDOM_STATE
            )
            model.fit(scaler.transform(X_train[train_idx]), y_train[train_idx])
            predictions = model.predict(scaler.transform(X_train[val_idx]))
            fold_accuracies.append(float(accuracy_score(y_train[val_idx], predictions)))
        mean_accuracy = float(np.mean(fold_accuracies))
        if mean_accuracy > best_mean_accuracy:
            best_mean_accuracy = mean_accuracy
            best_c = c
    return best_c


def _safe_roc_auc(y_true: FloatArray, y_proba: FloatArray) -> float:
    """`roc_auc_score` raises if `y_true` has only one class present (can
    happen on a small/degenerate OOS slice) — `0.5` (uninformative) is the
    honest value for "AUC isn't defined here", not an error."""
    if len(np.unique(y_true)) < 2:
        return 0.5
    return float(roc_auc_score(y_true, y_proba))


def train_and_validate_models(
    h1_bars: BarSeries,
    *,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    is_fraction: float = DEFAULT_IS_FRACTION,
) -> MLValidationReport:
    """Chronological in-sample (IS) / out-of-sample (OOS) split (matching
    `backtester/signal_validation.py`'s own methodology, for direct
    comparability), `TimeSeriesSplit`-selected logistic regression plus a
    fixed-hyperparameter `HistGradientBoostingClassifier` as a nonlinear
    comparison point, both evaluated exactly once on the untouched OOS
    slice. `naive_heuristic_oos_accuracy` re-runs `validate_signal()` over
    that identical OOS bar range, so every number in the returned report
    is measured on the same out-of-sample data.
    """
    X, y, first_valid_bar_index = prepare_training_data(h1_bars, horizon_bars=horizon_bars)
    n = len(y)
    if n < MIN_USABLE_SAMPLES:
        raise ValueError(
            f"need at least {MIN_USABLE_SAMPLES} usable bars to train/validate, got {n}"
        )

    split = int(n * is_fraction)
    X_train, y_train = X[:split], y[:split]
    X_oos, y_oos = X[split:], y[split:]

    best_c = _select_logistic_regression_c(X_train, y_train)
    scaler = StandardScaler().fit(X_train)
    lr_model = LogisticRegression(
        C=best_c, max_iter=LOGISTIC_REGRESSION_MAX_ITER, random_state=RANDOM_STATE
    )
    lr_model.fit(scaler.transform(X_train), y_train)
    lr_predictions = lr_model.predict(scaler.transform(X_oos))
    lr_probabilities = lr_model.predict_proba(scaler.transform(X_oos))[:, 1]
    lr_evaluation = ModelEvaluation(
        oos_accuracy=float(accuracy_score(y_oos, lr_predictions)),
        oos_precision=float(precision_score(y_oos, lr_predictions, zero_division=0)),
        oos_recall=float(recall_score(y_oos, lr_predictions, zero_division=0)),
        oos_roc_auc=_safe_roc_auc(y_oos, lr_probabilities),
    )
    raw_coefficients, raw_intercept = _fold_scaler_into_coefficients(
        lr_model.coef_[0], float(lr_model.intercept_[0]), scaler.mean_, scaler.scale_
    )
    logistic_regression = LogisticRegressionEvaluation(
        evaluation=lr_evaluation,
        raw_space_coefficients=raw_coefficients,
        raw_space_intercept=raw_intercept,
        selected_c=best_c,
    )

    gb_model = HistGradientBoostingClassifier(
        max_depth=GRADIENT_BOOSTING_MAX_DEPTH,
        max_iter=GRADIENT_BOOSTING_MAX_ITER,
        random_state=RANDOM_STATE,
    )
    gb_model.fit(X_train, y_train)
    gb_predictions = gb_model.predict(X_oos)
    gb_probabilities = gb_model.predict_proba(X_oos)[:, 1]
    gradient_boosting = ModelEvaluation(
        oos_accuracy=float(accuracy_score(y_oos, gb_predictions)),
        oos_precision=float(precision_score(y_oos, gb_predictions, zero_division=0)),
        oos_recall=float(recall_score(y_oos, gb_predictions, zero_division=0)),
        oos_roc_auc=_safe_roc_auc(y_oos, gb_probabilities),
    )

    majority_class_baseline = float(max(np.mean(y_oos), 1.0 - np.mean(y_oos)))
    oos_start_bar_index = first_valid_bar_index + split
    naive_heuristic_oos_accuracy = validate_signal(
        h1_bars, horizon_bars=horizon_bars, start_index=oos_start_bar_index
    ).accuracy

    return MLValidationReport(
        feature_names=FEATURE_NAMES,
        horizon_bars=horizon_bars,
        n_train=len(y_train),
        n_oos=len(y_oos),
        majority_class_baseline=majority_class_baseline,
        naive_heuristic_oos_accuracy=naive_heuristic_oos_accuracy,
        logistic_regression=logistic_regression,
        gradient_boosting=gradient_boosting,
    )


def binomial_ci_lower_bound(
    accuracy: float, n: int, *, confidence_z: float = CONFIDENCE_Z
) -> float:
    """Normal-approximation 95% (by default) binomial confidence interval's
    lower bound for an observed `accuracy` over `n` independent trials.
    `n <= 0` returns `0.0` (no evidence at all, the most conservative
    possible reading)."""
    if n <= 0:
        return 0.0
    margin = confidence_z * math.sqrt(accuracy * (1.0 - accuracy) / n)
    return accuracy - margin


def evaluate_promotion_bar(report: MLValidationReport) -> bool:
    """`True` iff the logistic-regression candidate — the only one
    deployable live without a runtime `sklearn` dependency, see this
    module's docstring — clears every one of:

    1. Its OOS accuracy's 95% binomial CI lower bound exceeds 50% (not
       just the point estimate — guards against a lucky OOS slice).
    2. Its OOS accuracy beats the majority-class baseline (guards against
       mistaking gold's directional drift for real indicator-based skill).
    3. Its OOS accuracy beats the existing naive heuristic's measured
       accuracy on this identical OOS range.
    4. Its OOS ROC-AUC exceeds `MIN_ROC_AUC_FOR_PROMOTION`. This gate was
       added after the other three let a genuinely degenerate model
       through on real data: `TimeSeriesSplit` selected maximum
       regularization (no learnable signal in-sample means "always
       predict close to the majority class" is the safest choice), which
       produced a logistic regression with ~99.97% recall and accuracy
       0.02 percentage points above the majority-class baseline — passing
       gates 1-3 on a technicality while its ROC-AUC sat at ~0.50 (zero
       real ranking power). Accuracy alone cannot distinguish "found a
       real edge" from "learned to mostly guess the more common class";
       AUC can, since it's insensitive to the base rate.

    Gradient boosting is deliberately not part of this gate: even if it
    outperforms, this module's design does not deploy a tree ensemble
    live (see `MLValidationReport`'s docstring / this module's header) —
    a passing gradient-boosting result on its own is reported, not acted
    on.
    """
    lr = report.logistic_regression
    ci_lower_bound = binomial_ci_lower_bound(lr.evaluation.oos_accuracy, report.n_oos)
    return (
        ci_lower_bound > 0.5
        and lr.evaluation.oos_accuracy > report.majority_class_baseline
        and lr.evaluation.oos_accuracy > report.naive_heuristic_oos_accuracy
        and lr.evaluation.oos_roc_auc > MIN_ROC_AUC_FOR_PROMOTION
    )
