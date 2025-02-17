__all__ = ["GLAD"]

from typing import Any, List, Optional, Tuple, cast

import attr
import numpy as np
import numpy.typing as npt
import pandas as pd
import scipy
import scipy.stats
from scipy.optimize import minimize
from tqdm.auto import tqdm

# logsumexp was moved to scipy.special in 0.19.0rc1 version of scipy
try:
    from scipy.special import logsumexp
except ImportError:
    from scipy.misc.common import logsumexp

from ..base import BaseClassificationAggregator
from ..utils import named_series_attrib


@attr.s
class GLAD(BaseClassificationAggregator):
    r"""The **GLAD** (Generative model of Labels, Abilities, and Difficulties) model is a probabilistic model that parametrizes the abilities of workers and the difficulty of tasks.

    Let's consider a case of $K$ class classification. Let $p$ be a vector of prior class probabilities,
    $\alpha_i \in (-\infty, +\infty)$ be a worker ability parameter, $\beta_j \in (0, +\infty)$ be
    an inverse task difficulty, $z_j$ be a latent variable representing the true task label, and $y^i_j$
    be a worker response that we observe. The relationships between these variables and parameters according
    to GLAD are represented by the following latent label model:

    ![GLAD latent label model](https://tlk.s3.yandex.net/crowd-kit/docs/glad_llm.png)


    The prior probability of $z_j$ being equal to $c$ is
    $$
    \operatorname{Pr}(z_j = c) = p[c],
    $$
    and the probability distribution of the worker responses with the true label $c$ follows the
    single coin Dawid-Skene model where the true label probability is a sigmoid function of the product of the
    worker ability and the inverse task difficulty:
    $$
    \operatorname{Pr}(y^i_j = k | z_j = c) = \begin{cases}a(i, j), & k = c \\ \frac{1 - a(i,j)}{K-1}, & k \neq c\end{cases},
    $$
    where
    $$
    a(i,j) = \frac{1}{1 + \exp(-\alpha_i\beta_j)}.
    $$

    Parameters $p$, $\alpha$, $\beta$, and latent variables $z$ are optimized with the Expectation-Minimization algorithm:
    1. **E-step**. Estimates the true task label probabilities using the alpha parameters of workers' abilities,
        the prior label probabilities, and the beta parameters of task difficulty.
    2. **M-step**. Optimizes the alpha and beta parameters using the conjugate gradient method.


    J. Whitehill, P. Ruvolo, T. Wu, J. Bergsma, and J. Movellan.
    Whose Vote Should Count More: Optimal Integration of Labels from Labelers of Unknown Expertise.
    *Proceedings of the 22nd International Conference on Neural Information Processing Systems*, 2009

    <https://proceedings.neurips.cc/paper/2009/file/f899139df5e1059396431415e770c6dd-Paper.pdf>


    Args:
        n_iter: The maximum number of EM iterations.
        tol: The tolerance stopping criterion for iterative methods with a variable number of steps.
            The algorithm converges when the loss change is less than the `tol` parameter.
        silent: Specifies if the progress bar will be shown (false) or not (true).
        labels_priors: The prior label probabilities.
        alphas_priors_mean: The prior mean value of the alpha parameters.
        betas_priors_mean: The prior mean value of the beta parameters.
        m_step_max_iter: The maximum number of iterations of the conjugate gradient method in the M-step.
        m_step_tol: The tolerance stopping criterion of the conjugate gradient method in the M-step.

    Examples:
        >>> from crowdkit.aggregation import GLAD
        >>> from crowdkit.datasets import load_dataset
        >>> df, gt = load_dataset('relevance-2')
        >>> glad = GLAD()
        >>> result = glad.fit_predict(df)

    Attributes:
        labels_ (typing.Optional[pandas.core.series.Series]): The task labels. The `pandas.Series` data is indexed by `task`
            so that `labels.loc[task]` is the most likely true label of tasks.

        probas_ (typing.Optional[pandas.core.frame.DataFrame]): The probability distributions of task labels.
            The `pandas.DataFrame` data is indexed by `task` so that `result.loc[task, label]` is the probability that the `task` true label is equal to `label`.
            Each probability is in the range from 0 to 1, all task probabilities must sum up to 1.

        alphas_ (Series): The alpha parameters of workers' abilities. The `pandas.Series` data is indexed by `worker`
            that contains the estimated alpha parameters.

        betas_ (Series): The beta parameters of task difficulty. The `pandas.Series` data is indexed by `task`
            that contains the estimated beta parameters.

        loss_history_ (List[float]): A list of loss values during training.
    """

    n_iter: int = attr.ib(default=100)
    tol: float = attr.ib(default=1e-5)
    silent: bool = attr.ib(default=True)
    labels_priors: Optional[pd.Series] = attr.ib(default=None)
    alphas_priors_mean: Optional[pd.Series] = attr.ib(default=None)
    betas_priors_mean: Optional[pd.Series] = attr.ib(default=None)
    m_step_max_iter: int = attr.ib(default=25)
    m_step_tol: float = attr.ib(default=1e-2)

    # Available after fit
    # labels_
    probas_: Optional[pd.DataFrame] = attr.ib(init=False)
    alphas_: pd.Series = named_series_attrib(name="alpha")
    betas_: pd.Series = named_series_attrib(name="beta")
    loss_history_: List[float] = attr.ib(init=False)

    def _join_all(
        self, data: pd.DataFrame, alphas: pd.Series, betas: pd.Series, priors: pd.Series
    ) -> pd.DataFrame:
        """Makes a data frame with format `(task, worker, label, variable) -> (alpha, beta, posterior, delta)`"""
        labels = list(priors.index)
        data = data.set_index("task")
        data[labels] = 0
        data.reset_index(inplace=True)
        data = data.melt(
            id_vars=["task", "worker", "label"],
            value_vars=labels,
            value_name="posterior",
        )
        data = data.set_index("variable")
        data.reset_index(inplace=True)
        data.set_index("task", inplace=True)
        data["beta"] = betas
        data = data.reset_index().set_index("worker")
        data["alpha"] = alphas
        data.reset_index(inplace=True)
        data["delta"] = data["label"] == data["variable"]
        return data

    def _e_step(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Performs E-step of GLAD algorithm.

        Estimates the true task label probabilities using the alpha parameters of workers' abilities,
        the prior label probabilities, and the beta parameters of task difficulty.
        """
        alpha_beta = data["alpha"] * np.exp(data["beta"])
        log_sigma = -self._softplus(-alpha_beta)
        log_one_minus_sigma = -self._softplus(alpha_beta)
        data["posterior"] = data["delta"] * log_sigma + (1 - data["delta"]) * (
            log_one_minus_sigma - np.log(len(self.prior_labels_) - 1)
        )
        # sum up by workers
        probas = data.groupby(["task", "variable"]).sum(numeric_only=True)["posterior"]
        # add priors to every label
        probas = probas.add(np.log(cast(pd.Series, self.priors_)), level=1)
        # exponentiate and normalize
        probas = probas.groupby(["task"]).transform(self._softmax)
        # put posterior in data['posterior']
        probas.name = "posterior"
        data = pd.merge(
            data.drop("posterior", axis=1), probas, on=["task", "variable"], copy=False
        )

        self.probas_ = probas.unstack()
        return data

    def _gradient_Q(
        self, data: pd.DataFrame
    ) -> Tuple[npt.NDArray[Any], npt.NDArray[Any]]:
        """Computes gradient of loss function"""

        sigma = scipy.special.expit(data["alpha"] * np.exp(data["beta"]))
        # multiply by exponent of beta because of beta -> exp(beta) reparameterization
        data["dQb"] = (
            data["posterior"]
            * (data["delta"] - sigma)
            * data["alpha"]
            * np.exp(data["beta"])
        )
        dQbeta = data.groupby("task").sum(numeric_only=True)["dQb"]
        # gradient of priors on betas
        dQbeta -= self.betas_ - self.betas_priors_mean_

        data["dQa"] = data["posterior"] * (data["delta"] - sigma) * np.exp(data["beta"])
        dQalpha = data.groupby("worker").sum(numeric_only=True)["dQa"]
        # gradient of priors on alphas
        dQalpha -= self.alphas_ - self.alphas_priors_mean_
        return dQalpha, dQbeta

    def _compute_Q(self, data: pd.DataFrame) -> float:
        """Computes loss function"""

        alpha_beta = data["alpha"] * np.exp(data["beta"])
        log_sigma = -self._softplus(-alpha_beta)
        log_one_minus_sigma = -self._softplus(alpha_beta)
        data["task_expectation"] = data["posterior"] * (
            data["delta"] * log_sigma
            + (1 - data["delta"])
            * (log_one_minus_sigma - np.log(len(self.prior_labels_) - 1))
        )
        Q = data["task_expectation"].sum()

        # priors on alphas and betas
        Q += np.log(scipy.stats.norm.pdf(self.alphas_ - self.alphas_priors_mean_)).sum()
        Q += np.log(scipy.stats.norm.pdf(self.betas_ - self.betas_priors_mean_)).sum()
        if np.isnan(Q):
            return -np.inf
        return float(Q)

    def _optimize_f(self, x: npt.NDArray[Any]) -> float:
        """Computes loss by parameters represented by numpy array"""
        alpha, beta = self._get_alphas_betas_by_point(x)
        self._update_alphas_betas(alpha, beta)
        return -self._compute_Q(self._current_data)

    def _optimize_df(self, x: npt.NDArray[Any]) -> npt.NDArray[Any]:
        """Computes loss gradient by parameters represented by numpy array"""
        alpha, beta = self._get_alphas_betas_by_point(x)
        self._update_alphas_betas(alpha, beta)
        dQalpha, dQbeta = self._gradient_Q(self._current_data)

        minus_grad = np.zeros_like(x)
        minus_grad[: len(self.workers_)] = -dQalpha[self.workers_].values
        minus_grad[len(self.workers_) :] = -dQbeta[self.tasks_].values
        return minus_grad

    def _update_alphas_betas(self, alphas: pd.Series, betas: pd.Series) -> None:
        self.alphas_ = alphas
        self.betas_ = betas
        self._current_data.set_index("worker", inplace=True)
        self._current_data["alpha"] = alphas
        self._current_data.reset_index(inplace=True)
        self._current_data.set_index("task", inplace=True)
        self._current_data["beta"] = betas
        self._current_data.reset_index(inplace=True)

    def _get_alphas_betas_by_point(
        self, x: npt.NDArray[Any]
    ) -> Tuple[pd.Series, pd.Series]:
        alphas = pd.Series(x[: len(self.workers_)], index=self.workers_, name="alpha")
        alphas.index.name = "worker"
        betas = pd.Series(x[len(self.workers_) :], index=self.tasks_, name="beta")
        betas.index.name = "task"
        return alphas, betas

    def _m_step(self, data: pd.DataFrame) -> pd.DataFrame:
        """Optimizes the alpha and beta parameters using the conjugate gradient method."""
        x_0 = np.concatenate([self.alphas_.values, self.betas_.values])  # type: ignore
        self._current_data = data
        res = minimize(
            self._optimize_f,
            x_0,
            method="CG",
            jac=self._optimize_df,
            tol=self.m_step_tol,
            options={"disp": False, "maxiter": self.m_step_max_iter},
        )
        self.alphas_, self.betas_ = self._get_alphas_betas_by_point(res.x)
        self._update_alphas_betas(self.alphas_, self.betas_)
        return self._current_data

    def _init(self, data: pd.DataFrame) -> None:
        self.alphas_ = pd.Series(1.0, index=pd.unique(data.worker))
        self.betas_ = pd.Series(1.0, index=pd.unique(data.task))
        self.tasks_ = pd.unique(data["task"])
        self.workers_ = pd.unique(data["worker"])
        self.priors_ = self.labels_priors
        if self.priors_ is None:
            self.prior_labels_ = pd.unique(data["label"])
            self.priors_ = pd.Series(
                1.0 / len(self.prior_labels_), index=self.prior_labels_
            )
        else:
            self.prior_labels_ = self.priors_.index
        self.alphas_priors_mean_ = self.alphas_priors_mean
        if self.alphas_priors_mean_ is None:
            self.alphas_priors_mean_ = pd.Series(1.0, index=self.alphas_.index)
        self.betas_priors_mean_ = self.betas_priors_mean
        if self.betas_priors_mean_ is None:
            self.betas_priors_mean_ = pd.Series(1.0, index=self.betas_.index)

    @staticmethod
    def _softplus(x: pd.Series, limit: int = 30) -> npt.NDArray[Any]:
        """log(1 + exp(x)) stable version

        For x > 30 or x < -30 error is less than 1e-13
        """
        positive_mask = x > limit
        negative_mask = x < -limit
        mask = positive_mask | negative_mask
        return cast(
            npt.NDArray[Any],
            np.log1p(np.exp(x * (1 - mask))) * (1 - mask) + x * positive_mask,
        )

    # backport for scipy < 1.12.0
    @staticmethod
    def _softmax(x: npt.NDArray[Any]) -> npt.NDArray[Any]:
        return cast(npt.NDArray[Any], np.exp(x - logsumexp(x, keepdims=True)))

    def fit(self, data: pd.DataFrame) -> "GLAD":
        """Fits the model to the training data with the EM algorithm.

        Args:
            data (DataFrame): The training dataset of workers' labeling results
                which is represented as the `pandas.DataFrame` data containing `task`, `worker`, and `label` columns.

        Returns:
            GLAD: self.
        """

        # Initialization
        data = data.filter(["task", "worker", "label"])
        self._init(data)
        data = self._join_all(data, self.alphas_, self.betas_, self.priors_)
        data = self._e_step(data)
        Q = self._compute_Q(data)

        self.loss_history_ = []
        iterations_range = (
            tqdm(range(self.n_iter)) if not self.silent else range(self.n_iter)
        )
        for _ in iterations_range:
            last_Q = Q
            if not self.silent:
                iterations_range.set_description(f"Q = {round(Q, 4)}")

            # E-step
            data = self._e_step(data)

            # M-step
            data = self._m_step(data)

            Q = self._compute_Q(data) / len(data)

            self.loss_history_.append(Q)
            if Q - last_Q < self.tol:
                break

        self.labels_ = cast(pd.DataFrame, self.probas_).idxmax(axis=1)
        return self

    def fit_predict_proba(self, data: pd.DataFrame) -> pd.DataFrame:
        """Fits the model to the training data and returns probability distributions of labels for each task.

        Args:
            data (DataFrame): The training dataset of workers' labeling results
                which is represented as the `pandas.DataFrame` data containing `task`, `worker`, and `label` columns.

        Returns:
            DataFrame: Probability distributions of task labels.
                The `pandas.DataFrame` data is indexed by `task` so that `result.loc[task, label]` is the probability that the `task` true label is equal to `label`.
                Each probability is in he range from 0 to 1, all task probabilities must sum up to 1.
        """

        return self.fit(data).probas_

    def fit_predict(self, data: pd.DataFrame) -> pd.Series:
        """Fits the model to the training data and returns the aggregated results.

        Args:
            data (DataFrame): The training dataset of workers' labeling results
                which is represented as the `pandas.DataFrame` data containing `task`, `worker`, and `label` columns.

        Returns:
            Series: Task labels. The `pandas.Series` data is indexed by `task`
                so that `labels.loc[task]` is the most likely true label of tasks.
        """

        return self.fit(data).labels_
