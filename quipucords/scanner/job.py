"""ScanJobRunner runs a group of scan tasks."""
import logging
from multiprocessing import Process, Value

from django.db.models import Q

from api.common.common_report import create_report_version
from api.details_report.util import (
    build_sources_from_tasks,
    create_details_report,
    validate_details_report_json,
)
from api.models import ScanJob, ScanTask, Source
from fingerprinter.task import FingerprintTaskRunner
from scanner import network, openshift, satellite, vcenter

# Get an instance of a logger
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


MODULE_PER_SOURCE_TYPE = {
    Source.NETWORK_SOURCE_TYPE: network,
    Source.OPENSHIFT_SOURCE_TYPE: openshift,
    Source.SATELLITE_SOURCE_TYPE: satellite,
    Source.VCENTER_SOURCE_TYPE: vcenter,
}


class ScanJobRunner(Process):
    """ScanProcess perform a group of scan tasks."""

    def __init__(self, scan_job):
        """Create discovery scanner."""
        Process.__init__(self)
        self.scan_job = scan_job
        self.identifier = scan_job.id
        self.manager_interrupt = Value("i", ScanJob.JOB_RUN)

    def run(self):
        """Trigger thread execution."""
        # pylint: disable=inconsistent-return-statements
        # pylint: disable=no-else-return
        # pylint: disable=too-many-locals,too-many-statements
        # pylint: disable=too-many-return-statements,too-many-branches
        # check to see if manager killed job
        if self.manager_interrupt.value == ScanJob.JOB_TERMINATE_CANCEL:
            self.manager_interrupt.value = ScanJob.JOB_TERMINATE_ACK
            self.scan_job.cancel()
            return ScanTask.CANCELED

        if self.manager_interrupt.value == ScanJob.JOB_TERMINATE_PAUSE:
            self.manager_interrupt.value = ScanJob.JOB_TERMINATE_ACK
            self.scan_job.pause()
            return ScanTask.PAUSED

        # Job is not running so start
        self.scan_job.start()
        if self.scan_job.status != ScanTask.RUNNING:
            error_message = (
                "Job could not transition to running state.  See error logs."
            )
            self.scan_job.fail(error_message)
            return ScanTask.FAILED

        # Load tasks that have no been run or are in progress
        task_runners = []

        incomplete_scan_tasks = self.scan_job.tasks.filter(
            Q(status=ScanTask.RUNNING) | Q(status=ScanTask.PENDING)
        ).order_by("sequence_number")
        fingerprint_task_runner = None
        for scan_task in incomplete_scan_tasks:
            runner = self._create_task_runner(scan_task)
            if not runner:
                error_message = (
                    "Scan task has not recognized"
                    f" type/source combination: {scan_task}"
                )

                scan_task.fail(error_message)
                self.scan_job.fail(error_message)
                return ScanTask.FAILED

            if isinstance(runner, FingerprintTaskRunner):
                fingerprint_task_runner = runner
            else:
                task_runners.append(runner)

        self.scan_job.log_message(
            f"Job has {len(incomplete_scan_tasks):d} remaining tasks"
        )

        failed_tasks = []
        for runner in task_runners:
            # Mark runner as running

            task_status = self._run_task(runner)

            if task_status == ScanTask.FAILED:
                # Task did not complete successfully
                failed_tasks.append(runner.scan_task)
            elif task_status != ScanTask.COMPLETED:
                # something went wrong or cancel/pause
                return task_status

        if self.scan_job.scan_type in [
            ScanTask.SCAN_TYPE_INSPECT,
            ScanTask.SCAN_TYPE_FINGERPRINT,
        ]:
            details_report = fingerprint_task_runner.scan_task.details_report

            if not details_report:
                # Create the details report
                has_errors, details_report = self._create_details_report()
                if has_errors:
                    return ScanTask.FAILED

            if not details_report:
                self.scan_job.fail("No facts gathered from scan.")
                return ScanTask.FAILED

            # Associate details report with scan job
            self.scan_job.details_report = details_report
            self.scan_job.save()

            # Associate details report with fingerprint task
            fingerprint_task_runner.scan_task.details_report = details_report
            fingerprint_task_runner.scan_task.save()
            try:
                task_status = self._run_task(fingerprint_task_runner)
            except Exception as error:
                fingerprint_task_runner.scan_task.log_message(
                    "DETAILS REPORT - "
                    "The following details report failed to generate a"
                    f" deployments report: {details_report}",
                    log_level=logging.ERROR,
                    exception=error,
                )
                raise error
            if task_status in [ScanTask.CANCELED, ScanTask.PAUSED]:
                return task_status
            elif task_status != ScanTask.COMPLETED:
                # Task did not complete successfully
                failed_tasks.append(fingerprint_task_runner.scan_task)
                fingerprint_task_runner.scan_task.log_message(
                    f"Task {fingerprint_task_runner.scan_task.sequence_number} failed.",
                    log_level=logging.ERROR,
                )
                fingerprint_task_runner.scan_task.log_message(
                    "DETAILS REPORT - "
                    "The following details report failed to generate a"
                    f" deployments report: {details_report}",
                    log_level=logging.ERROR,
                )
            else:
                # Record results for successful tasks
                self.scan_job.report_id = details_report.deployment_report.id
                self.scan_job.save()
                self.scan_job.log_message(
                    f"Report {self.scan_job.report_id:d} created."
                )

        if failed_tasks:
            failed_task_ids = ", ".join(
                [str(task.sequence_number) for task in failed_tasks]
            )
            error_message = f"The following tasks failed: {failed_task_ids}"
            self.scan_job.fail(error_message)
            return ScanTask.FAILED

        self.scan_job.complete()
        return ScanTask.COMPLETED

    def _create_task_runner(self, scan_task):
        """Create ScanTaskRunner using scan_type and source_type."""
        # pylint: disable=no-else-return
        scan_type = scan_task.scan_type
        if scan_type == ScanTask.SCAN_TYPE_CONNECT:
            return self._create_connect_task_runner(scan_task)
        elif scan_type == ScanTask.SCAN_TYPE_INSPECT:
            return self._create_inspect_task_runner(scan_task)
        elif scan_type == ScanTask.SCAN_TYPE_FINGERPRINT:
            return FingerprintTaskRunner(self.scan_job, scan_task)
        return None

    def _run_task(self, runner):
        """Run a sigle scan task."""
        # pylint: disable=no-else-return
        if self.manager_interrupt.value == ScanJob.JOB_TERMINATE_CANCEL:
            self.manager_interrupt.value = ScanJob.JOB_TERMINATE_ACK
            return ScanTask.CANCELED

        if self.manager_interrupt.value == ScanJob.JOB_TERMINATE_PAUSE:
            self.manager_interrupt.value = ScanJob.JOB_TERMINATE_ACK
            return ScanTask.PAUSED
        runner.scan_task.start()
        # run runner
        try:
            status_message, task_status = runner.run(self.manager_interrupt)
        except Exception as error:
            failed_task = runner.scan_task
            context_message = "Unexpected failure occurred."
            context_message += "See context below.\n"
            context_message += f"SCAN JOB: {self.scan_job}\n"
            context_message += f"TASK: {failed_task}\n"
            if failed_task.scan_type != ScanTask.SCAN_TYPE_FINGERPRINT:
                context_message += f"SOURCE: {failed_task.source}\n"
                creds = [str(cred) for cred in failed_task.source.credentials.all()]
                context_message += f"CREDENTIALS: [{creds}]"
            failed_task.fail(context_message)

            message = f"FATAL ERROR. {str(error)}"
            self.scan_job.fail(message)
            raise error

        # Save Task status
        if task_status == ScanTask.CANCELED:
            runner.scan_task.cancel()
            runner.scan_job.cancel()
        elif task_status == ScanTask.PAUSED:
            runner.scan_task.pause()
            runner.scan_job.pause()
        elif task_status == ScanTask.COMPLETED:
            runner.scan_task.complete(status_message)
        elif task_status == ScanTask.FAILED:
            runner.scan_task.fail(status_message)
        else:
            error_message = (
                f"ScanTask {runner.scan_task.sequence_number:d} failed."
                " Scan task must return"
                " ScanTask.COMPLETED or ScanTask.FAILED. ScanTask returned"
                f' "{task_status}" and the following status message: {status_message}'
            )
            runner.scan_task.fail(error_message)
            task_status = ScanTask.FAILED
        return task_status

    @classmethod
    def _get_source_module(cls, scan_task: ScanTask):
        """Get the appropriate module for scan task based on its source type."""
        source_type = scan_task.source.source_type
        try:
            return MODULE_PER_SOURCE_TYPE[source_type]
        except KeyError as error:
            raise NotImplementedError(
                f"Unsupported source type: {source_type}"
            ) from error

    def _create_connect_task_runner(self, scan_task):
        """Create connection TaskRunner using source_type."""
        module = self._get_source_module(scan_task)
        return module.ConnectTaskRunner(self.scan_job, scan_task)

    def _create_inspect_task_runner(self, scan_task):
        """Create inspection TaskRunner using source_type."""
        module = self._get_source_module(scan_task)
        return module.InspectTaskRunner(self.scan_job, scan_task)

    def _create_details_report(self):
        """Send collected host scan facts to fact endpoint.

        :param facts: The array of fact dictionaries
        :returns: bool indicating if there are errors and dict with result.
        """
        inspect_tasks = self.scan_job.tasks.filter(
            scan_type=ScanTask.SCAN_TYPE_INSPECT
        ).order_by("sequence_number")
        sources = build_sources_from_tasks(
            inspect_tasks.filter(status=ScanTask.COMPLETED)
        )
        if bool(sources):
            details_report_json = {
                "sources": sources,
                "report_type": "details",
                "report_version": create_report_version(),
            }
            has_errors, validation_result = validate_details_report_json(
                details_report_json, False
            )

            if has_errors:
                message = (
                    f"Scan produced invalid details report JSON: {validation_result}"
                )
                self.scan_job.fail(message)
                return True, {}

            # Create FC model and save data to JSON file
            details_report = create_details_report(
                create_report_version(), details_report_json
            )
            return False, details_report
        message = "No connection results found."
        self.scan_job.fail(message)
        return True, {}

    def __str__(self):
        """Convert to string."""
        return f"{{scan_job:{self.scan_job.id}, }}"
