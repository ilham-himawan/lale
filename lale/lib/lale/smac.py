# Copyright 2019 IBM Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import numpy as np
import sys

import time
import traceback
from sklearn.model_selection import train_test_split
from sklearn.model_selection._split import check_cv
from sklearn.metrics import log_loss
from sklearn.metrics.scorer import check_scoring

# Import ConfigSpace and different types of parameters
from smac.configspace import ConfigurationSpace

# Import SMAC-utilities
from smac.facade.smac_facade import SMAC as orig_SMAC
from smac.scenario.scenario import Scenario
from smac.tae.execute_ta_run import BudgetExhaustedException
from lale.helpers import cross_val_score_track_trials
from lale.lib.sklearn import LogisticRegression
import lale.operators
from lale.search.lale_smac import lale_op_smac_tae, get_smac_space, lale_trainable_op_from_config
import lale.sklearn_compat

logger = logging.getLogger(__name__)

class SMACImpl:

    def __init__(self, estimator=None, max_evals=50, cv=5, handle_cv_failure=False, scoring='accuracy', best_score=0.0, max_opt_time=None, lale_num_grids=None):
        """ Instantiate the SMAC that will use the given estimator and other parameters to select the 
        best performing trainable instantiation of the estimator. 

        Parameters
        ----------
        estimator : lale.operators.IndividualOp or lale.operators.Pipeline, optional
            A valid Lale individual operator or pipeline, by default LogisticRegression
        max_evals : int, optional
            Number of trials of SMAC search i.e. runcount_limit of SMAC, by default 50
        cv : an integer or an object that has a split function as a generator yielding (train, test) splits as arrays of indices.
            Integer value is used as number of folds in sklearn.model_selection.StratifiedKFold, default is 5.
            Note that any of the iterators from https://scikit-learn.org/stable/modules/cross_validation.html#cross-validation-iterators can be used here.
            The fit method performs cross validation on the input dataset for per trial, 
            and uses the mean cross validation performance for optimization. This behavior is also impacted by handle_cv_failure flag, 
            by default 5
        handle_cv_failure : bool, optional
            A boolean flag to indicating how to deal with cross validation failure for a trial.
            If True, the trial is continued by doing a 80-20 percent train-validation split of the dataset input to fit
            and reporting the score on the validation part.
            If False, the trial is terminated by assigning status to FAIL.
            , by default False
        scoring: string or a scorer object created using 
            https://scikit-learn.org/stable/modules/generated/sklearn.metrics.make_scorer.html#sklearn.metrics.make_scorer.
            A string from sklearn.metrics.SCORERS.keys() can be used or a scorer created from one of 
            sklearn.metrics (https://scikit-learn.org/stable/modules/classes.html#module-sklearn.metrics).
            A completely custom scorer object can be created from a python function following the example at 
            https://scikit-learn.org/stable/modules/model_evaluation.html
            The metric has to return a scalar value, and note that scikit-learns's scorer object always returns values such that
            higher score is better. Since Hyperopt solves a minimization problem, we pass (best_score - score) to Hyperopt.
            by default 'accuracy'.
        best_score : float, optional
            The best score for the specified scorer. This allows us to return a loss to hyperopt that is
            greater than equal to zero, where zero is the best loss. By default, zero.
        max_opt_time : float, optional
            Maximum amount of wall clock time in seconds for the optimization. By default, None, implying no runtime
            bound.

        Examples
        --------
        >>> from sklearn.metrics import make_scorer, f1_score, accuracy_score
        >>> lr = LogisticRegression()
        >>> clf = SMAC(estimator=lr, scoring='accuracy', cv=5)
        >>> from sklearn import datasets
        >>> diabetes = datasets.load_diabetes()
        >>> X = diabetes.data[:150]
        >>> y = diabetes.target[:150]
        >>> trained = clf.fit(X, y)
        >>> predictions = trained.predict(X)

        Other scoring metrics:

        >>> clf = SMAC(estimator=lr, scoring=make_scorer(f1_score, average='macro'), cv=3, max_evals=2)

        """

        self.max_evals = max_evals
        if estimator is None:
            self.estimator = LogisticRegression()
        else:
            self.estimator = estimator

        self.search_space:ConfigurationSpace = get_smac_space(self.estimator, lale_num_grids=lale_num_grids)
        self.scoring = scoring
        self.best_score = best_score
        self.handle_cv_failure = handle_cv_failure
        self.cv = cv
        self.max_opt_time = max_opt_time
        # Scenario object
        scenario_options = {"run_obj": "quality",   # we optimize quality (alternatively runtime)
                            "runcount-limit": self.max_evals,  # maximum function evaluations
                            "cs": self.search_space,               # configuration space
                            "deterministic": "true",
                            "abort_on_first_run_crash": False,
                            }
        if max_opt_time is not None:
            scenario_options["wallclock_limit"]= max_opt_time
        self.scenario = Scenario(scenario_options)
        self.trials = None

    def fit(self, X_train, y_train):
        self.cv = check_cv(self.cv, y = y_train, classifier=True) #TODO: Replace the classifier flag value by using tags?

        def smac_train_test(trainable, X_train, y_train):
            try:
                cv_score, logloss, execution_time = cross_val_score_track_trials(trainable, X_train, y_train, cv=self.cv, scoring=self.scoring)
                logger.debug("Successful trial of SMAC")
            except BaseException as e:
                #If there is any error in cross validation, use the score based on a random train-test split as the evaluation criterion
                if self.handle_cv_failure:
                    X_train_part, X_validation, y_train_part, y_validation = train_test_split(X_train, y_train, test_size=0.20)
                    start = time.time()
                    trained = trainable.fit(X_train_part, y_train_part)
                    scorer = check_scoring(trainable, scoring=self.scoring)
                    cv_score  = scorer(trained, X_validation, y_validation)
                    execution_time = time.time() - start
                    y_pred_proba = trained.predict_proba(X_validation)
                    try:
                        logloss = log_loss(y_true=y_validation, y_pred=y_pred_proba)
                    except BaseException:
                        logloss = 0
                        logger.debug("Warning, log loss cannot be computed")
                else:
                    logger.debug("Error {} with pipeline:{}".format(e, trainable.to_json()))
                    raise e
            return cv_score, logloss, execution_time

        def f(trainable):
            return_dict = {}
            try:
                score, logloss, execution_time = smac_train_test(trainable, X_train=X_train, y_train=y_train)
                return_dict = {
                    'loss': self.best_score - score,
                    'time': execution_time,
                    'log_loss': logloss
                }
            except BaseException as e:
                logger.warning(f"Exception caught in SMACCV:{type(e)}, {traceback.format_exc()}, SMAC will set a cost_for_crash to MAXINT.")
                raise e
            return return_dict['loss']

        try :
            smac = orig_SMAC(scenario=self.scenario, rng=np.random.RandomState(42),
                    tae_runner=lale_op_smac_tae(self.estimator, f))
            incumbent = smac.optimize()
            self.trials = smac.get_runhistory()
            trainable = lale_trainable_op_from_config(self.estimator, incumbent)
            #get the trainable corresponding to the best params and train it on the entire training dataset.
            trained = trainable.fit(X_train, y_train)
            self._best_estimator = trained
        except BudgetExhaustedException:
            logger.warning('Maximum alloted optimization time exceeded. Optimization exited prematurely')
        except BaseException as e:
            logger.warning('Error during optimization: {}'.format(e))
            self._best_estimator = None

        return self

    def predict(self, X_eval):
        import warnings
        warnings.filterwarnings("ignore")
        trained = self._best_estimator
        try:
            predictions = trained.predict(X_eval)
        except ValueError as e:
            logger.warning("ValueError in predicting using SMACCV:{}, the error is:{}".format(trained, e))
            predictions = None

        return predictions

    def get_trials(self):
        """Returns the trials i.e. RunHistory object.
        
        Returns
        -------
        smac.runhistory.runhistory.RunHistory
            RunHistory of all the trials executed during the optimization i.e. fit method of SMACCV.
        """
        return self.trials

    def get_pipeline(self, pipeline_name=None, astype='lale'):
        if pipeline_name is not None:
            raise NotImplementedError('Cannot get pipeline by name yet.')
        result = getattr(self, '_best_estimator', None)
        if result is None or astype == 'lale':
            return result
        assert astype == 'sklearn', astype
        return lale.sklearn_compat.make_sklearn_compat(result)

_hyperparams_schema = {
    'allOf': [
    {   'type': 'object',
        'required': [
            'estimator', 'max_evals', 'cv', 'handle_cv_failure',
            'max_opt_time', 'lale_num_grids'],
        'relevantToOptimizer': ['estimator'],
        'additionalProperties': False,
        'properties': {
            'estimator': {
                'anyOf': [
                {   'laleType': 'operator',
                    'not': {'enum': [None]}},
                {   'enum': [None]}],
                'default': None},
            'max_evals': {
                'type': 'integer',
                'minimum': 1,
                'default': 50},
            'cv': {
                'type': 'integer',
                'minimum': 1,
                'default': 5},
            'handle_cv_failure': {
                'type': 'boolean',
                'default': False},
            'scoring': {
                'anyOf': [
                {    'description': 'Custom scorer object, see https://scikit-learn.org/stable/modules/model_evaluation.html',
                     'not': {'type': 'string'}},
                {    'enum': [
                        'accuracy', 'explained_variance', 'max_error',
                        'roc_auc', 'roc_auc_ovr', 'roc_auc_ovo',
                        'roc_auc_ovr_weighted', 'roc_auc_ovo_weighted',
                        'balanced_accuracy', 'average_precision',
                        'neg_log_loss', 'neg_brier_score', 'r2', 'neg_mean_squared_error', 'neg_mean_absolute_error',
                         'neg_root_mean_squared_error', 'neg_mean_squared_log_error',
                         'neg_median_absolute_error']}],
                'default': 'accuracy'},
            'best_score': {
                'type': 'number',
                'default': 0.0},
            'max_opt_time': {
                'anyOf': [
                {   'type': 'number',
                    'minimum': 0.0},
                {   'enum': [None]}],
                'default': None},
            'lale_num_grids': {
                'anyOf': [
                {   'description': 'If not set, keep all grids.',
                    'enum': [None]},
                {   'description': 'Fraction of grids to keep.',
                    'type': 'number',
                    'minimum': 0.0,
                    'exclusiveMinimum': True,
                    'maximum': 1.0,
                    'exclusiveMaximum': True},
                {   'description': 'Number of grids to keep.',
                    'type': 'integer',
                    'minimum': 1}],
                'default': None}
                }}]}

_input_fit_schema = {
    'type': 'object',
    'required': ['X', 'y'],
    'properties': {
        'X': {
            'type': 'array',
            'items': {
                'anyOf': [
                {   'type': 'array', 'items': {'type': ['number', 'string']}},
                {   'type': 'string'}]}},
        'y': {
            'type': 'array', 'items': {'type': 'number'}}}}

_input_predict_schema = {
    'type': 'object',
    'properties': {
        'X': {
            'type': 'array',
            'items': {
                'anyOf': [
                {   'type': 'array', 'items': {'type': ['number', 'string']}},
                {   'type': 'string'}]}}}}

_output_predict_schema = {
    'type': 'array', 'items': {'type': 'number'}}

_combined_schemas = {
    'documentation_url': 'https://lale.readthedocs.io/en/latest/modules/lale.lib.lale.hyperopt_cv.html',
    'type': 'object',
    'tags': {
        'pre': [],
        'op': ['estimator'],
        'post': []},
    'properties': {
        'hyperparams': _hyperparams_schema,
        'input_fit': _input_fit_schema,
        'input_predict': _input_predict_schema,
        'output_predict': _output_predict_schema}}

SMAC = lale.operators.make_operator(SMACImpl, _combined_schemas)
