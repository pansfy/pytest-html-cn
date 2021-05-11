# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import bisect
import datetime
import json
import os
import re
import smtplib
import time
import warnings
from base64 import b64encode, b64decode
from collections import OrderedDict
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from os.path import isfile

import pkg_resources
import pytest

try:
    from ansi2html import Ansi2HTMLConverter, style

    ANSI = True
except ImportError:
    # ansi2html is not installed
    ANSI = False

from py.xml import html, raw

from . import extras
from . import __version__, __pypi_url__


def pytest_addhooks(pluginmanager):
    from . import hooks

    pluginmanager.add_hookspecs(hooks)


def pytest_addoption(parser):
    group = parser.getgroup("terminal reporting")
    group.addoption(
        "--html",
        action="store",
        dest="htmlpath",
        metavar="path",
        default=None,
        help="create html report file at given path.",
    )
    group.addoption(
        "--self-contained-html",
        action="store_true",
        help="create a self-contained html file containing all "
             "necessary styles, scripts, and images - this means "
             "that the report may not render or function where CSP "
             "restrictions are in place (see "
             "https://developer.mozilla.org/docs/Web/Security/CSP)",
    )
    group.addoption(
        "--css",
        action="append",
        metavar="path",
        default=[],
        help="append given css file content to report style file.",
    )
    parser.addini(
        "render_collapsed",
        type="bool",
        default=False,
        help="Open the report with all rows collapsed. Useful for very large reports",
    )


def pytest_configure(config):
    htmlpath = config.getoption("htmlpath")
    if htmlpath:
        for csspath in config.getoption("css"):
            if not os.path.exists(csspath):
                raise IOError(f"找不到此文件或目录: '{csspath}'")
        if not hasattr(config, "slaveinput"):
            # prevent opening htmlpath on slave nodes (xdist)
            config._html = HTMLReport(htmlpath, config)
            config.pluginmanager.register(config._html)


def pytest_unconfigure(config):
    html = getattr(config, "_html", None)
    if html:
        del config._html
        config.pluginmanager.unregister(html)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        fixture_extras = getattr(item.config, "extras", [])
        plugin_extras = getattr(report, "extra", [])
        report.extra = fixture_extras + plugin_extras


@pytest.fixture
def extra(pytestconfig):
    """Add details to the HTML reports.

    .. code-block:: python

        import pytest_html
        def test_foo(extra):
            extra.append(pytest_html.extras.url('http://www.example.com/'))
    """
    pytestconfig.extras = []
    yield pytestconfig.extras
    del pytestconfig.extras[:]


def data_uri(content, mime_type="text/plain", charset="utf-8"):
    data = b64encode(content.encode(charset)).decode("ascii")
    return f"data:{mime_type};charset={charset};base64,{data}"


class PATH:
    """ all file PATH meta """
    resources_path = os.path.join(os.path.dirname(__file__), 'resources')
    mail_tmp_path = os.path.join(resources_path, 'mail.html')


class MailResult:
    """
    Test run results
    """
    title = ''
    count = 0
    passed = 0
    xpassed = 0
    failed = 0
    xfailed = 0
    errors = 0
    skipped = 0
    rerun = 0


TEST_ENV = {
    "ProjectName": "项目名称",
    "ProjectURL": "项目地址",
    "Python": "Python",
    "Platform": "操作系统",
    "Packages": "依赖包",
    "Plugins": "扩展插件"
}

TEST_STATUS = {
    "Passed": "通过",
    "Failed": "失败",
    "XPassed": "未知的通过",
    "XFailed": "预期的失败",
    "Error": "故障",
    "Skipped": "跳过"
}


class HTMLReport:
    def __init__(self, report_file, config):
        report_file = os.path.expanduser(os.path.expandvars(report_file))
        self.report_file = os.path.abspath(report_file)
        self.title = '测试报告'
        self.description = "本次测试描述"
        self.results = []
        self.test_logs = []
        self.errors = self.failed = 0
        self.passed = self.skipped = 0
        self.xfailed = self.xpassed = 0
        has_rerun = config.pluginmanager.hasplugin("rerunfailures")
        self.rerun = 0 if has_rerun else None
        self.self_contained = config.getoption("self_contained_html")
        self.config = config

    class TestResult:
        def __init__(self, outcome, report, report_file, config):
            self.test_id = report.nodeid.encode("utf-8").decode("unicode_escape")
            if getattr(report, "when", "call") != "call":
                self.test_id = "::".join([report.nodeid, report.when])
            self.time = getattr(report, "duration", 0.0)
            self.outcome = outcome
            self.additional_html = []
            self.links_html = []
            self.self_contained = config.getoption("self_contained_html")
            self.report_file = report_file
            self.config = config
            self.row_table = self.row_extra = None

            test_index = hasattr(report, "rerun") and report.rerun + 1 or 0

            for extra_index, extra in enumerate(getattr(report, "extra", [])):
                self.append_extra_html(extra, extra_index, test_index)

            self.append_log_html(report, self.additional_html)

            cells = [
                html.td(self.test_id, class_="col-name"),
                html.td(f"{self.time:.2f}", class_="col-duration"),
                html.td(TEST_STATUS.get(self.outcome), class_="col-result"),
                html.td(self.links_html, class_="col-links"),
            ]

            self.config.hook.pytest_html_results_table_row(report=report, cells=cells)

            self.config.hook.pytest_html_results_table_html(
                report=report, data=self.additional_html
            )

            if len(cells) > 0:
                tr_class = None
                if self.config.getini("render_collapsed"):
                    tr_class = "collapsed"
                self.row_table = html.tr(cells)
                self.row_extra = html.tr(
                    html.td(self.additional_html, class_="extra", colspan=len(cells)),
                    class_=tr_class,
                )

        def __lt__(self, other):
            order = (
                "Error",
                "Failed",
                "Rerun",
                "XFailed",
                "XPassed",
                "Skipped",
                "Passed",
            )
            return order.index(self.outcome) < order.index(other.outcome)

        def create_asset(
                self, content, extra_index, test_index, file_extension, mode="w"
        ):
            # 255 is the common max filename length on various filesystems
            asset_file_name = "{}_{}_{}.{}".format(
                re.sub(r"[^\w\.]", "_", self.test_id),
                str(extra_index),
                str(test_index),
                file_extension,
            )[-255:]
            asset_path = os.path.join(
                os.path.dirname(self.report_file), "assets", asset_file_name
            )

            if not os.path.exists(os.path.dirname(asset_path)):
                os.makedirs(os.path.dirname(asset_path))

            relative_path = f"assets/{asset_file_name}"

            kwargs = {"encoding": "utf-8"} if "b" not in mode else {}
            with open(asset_path, mode, **kwargs) as f:
                f.write(content)
            return relative_path

        def append_extra_html(self, extra, extra_index, test_index):
            href = None
            if extra.get("format") == extras.FORMAT_IMAGE:
                self._append_image(extra, extra_index, test_index)

            elif extra.get("format") == extras.FORMAT_HTML:
                self.additional_html.append(html.div(raw(extra.get("content"))))

            elif extra.get("format") == extras.FORMAT_JSON:
                content = json.dumps(extra.get("content"))
                if self.self_contained:
                    href = data_uri(content, mime_type=extra.get("mime_type"))
                else:
                    href = self.create_asset(
                        content, extra_index, test_index, extra.get("extension")
                    )

            elif extra.get("format") == extras.FORMAT_TEXT:
                content = extra.get("content")
                if isinstance(content, bytes):
                    content = content.decode("utf-8")
                if self.self_contained:
                    href = data_uri(content)
                else:
                    href = self.create_asset(
                        content, extra_index, test_index, extra.get("extension")
                    )

            elif extra.get("format") == extras.FORMAT_URL:
                href = extra.get("content")

            elif extra.get("format") == extras.FORMAT_VIDEO:
                self._append_video(extra, extra_index, test_index)

            if href is not None:
                self.links_html.append(
                    html.a(
                        extra.get("name"),
                        class_=extra.get("format"),
                        href=href,
                        target="_blank",
                    )
                )
                self.links_html.append(" ")

        def append_log_html(self, report, additional_html):
            log = html.div(class_="log")
            if report.longrepr:
                for line in report.longreprtext.splitlines():
                    separator = line.startswith("_ " * 10)
                    if separator:
                        log.append(line[:80])
                    else:
                        exception = line.startswith("E   ")
                        if exception:
                            log.append(html.span(raw(escape(line)), class_="error"))
                        else:
                            log.append(raw(escape(line)))
                    log.append(html.br())

            for section in report.sections:
                header, content = map(escape, section)
                log.append(f" {header:-^80} ")
                log.append(html.br())
                if ANSI:
                    converter = Ansi2HTMLConverter(inline=False, escaped=False)
                    content = converter.convert(content, full=False)
                log.append(raw(content))
                log.append(html.br())

            if len(log) == 0:
                log = html.div(class_="empty log")
                log.append("未捕获到日志.")
            additional_html.append(log)

        def _make_media_html_div(
                self, extra, extra_index, test_index, base_extra_string, base_extra_class
        ):
            content = extra.get("content")
            try:
                is_uri_or_path = content.startswith(("file", "http")) or isfile(content)
            except ValueError:
                # On Windows, os.path.isfile throws this exception when
                # passed a b64 encoded image.
                is_uri_or_path = False
            if is_uri_or_path:
                if self.self_contained:
                    warnings.warn(f"独立的 HTML 报告, 包含所有外链资源，资源: {content}")

                html_div = html.a(
                    raw(base_extra_string.format(extra.get("content"))), href=content
                )
            elif self.self_contained:
                src = f"data:{extra.get('mime_type')};base64,{content}"
                html_div = raw(base_extra_string.format(src))
            else:
                content = b64decode(content.encode("utf-8"))
                href = src = self.create_asset(
                    content, extra_index, test_index, extra.get("extension"), "wb"
                )
                html_div = html.a(class_=base_extra_class, target="_blank", href=href)
            return html_div

        def _append_image(self, extra, extra_index, test_index):
            image_base = '<img src="{}"/>'
            html_div = self._make_media_html_div(
                extra, extra_index, test_index, image_base, "image"
            )
            self.additional_html.append(html.div(html_div, class_="image"))

        def _append_video(self, extra, extra_index, test_index):
            video_base = '<video controls><source src="{}" type="video/mp4"></video>'
            html_div = self._make_media_html_div(
                extra, extra_index, test_index, video_base, "video"
            )
            self.additional_html.append(html.div(html_div, class_="video"))

    def _appendrow(self, outcome, report):
        result = self.TestResult(outcome, report, self.report_file, self.config)
        if result.row_table is not None:
            index = bisect.bisect_right(self.results, result)
            self.results.insert(index, result)
            tbody = html.tbody(
                result.row_table,
                class_="{} results-table-row".format(result.outcome.lower()),
            )
            if result.row_extra is not None:
                tbody.append(result.row_extra)
            self.test_logs.insert(index, tbody)

    def append_passed(self, report):
        if report.when == "call":
            if hasattr(report, "wasxfail"):
                self.xpassed += 1
                self._appendrow("XPassed", report)
            else:
                self.passed += 1
                self._appendrow("Passed", report)

    def append_failed(self, report):
        if getattr(report, "when", None) == "call":
            if hasattr(report, "wasxfail"):
                # pytest < 3.0 marked xpasses as failures
                self.xpassed += 1
                self._appendrow("XPassed", report)
            else:
                self.failed += 1
                self._appendrow("Failed", report)
        else:
            self.errors += 1
            self._appendrow("Error", report)

    def append_skipped(self, report):
        if hasattr(report, "wasxfail"):
            self.xfailed += 1
            self._appendrow("XFailed", report)
        else:
            self.skipped += 1
            self._appendrow("Skipped", report)

    def append_other(self, report):
        # For now, the only "other" the plugin give support is rerun
        self.rerun += 1
        self._appendrow("Rerun", report)

    def _generate_report(self, session):
        suite_stop_time = time.time()
        suite_time_delta = suite_stop_time - self.suite_start_time
        numtests = self.passed + self.failed + self.xpassed + self.xfailed
        generated = datetime.datetime.now()

        class Outcome:
            def __init__(
                    self, outcome, total=0, label=None, test_result=None, class_html=None
            ):
                self.outcome = outcome
                self.label = label or outcome
                self.class_html = class_html or outcome
                self.total = total
                self.test_result = test_result or outcome

                self.generate_checkbox()
                self.generate_summary_item()

            def generate_checkbox(self):
                checkbox_kwargs = {"data-test-result": self.test_result.lower()}
                if self.total == 0:
                    checkbox_kwargs["disabled"] = "true"

                self.checkbox = html.input(
                    type="checkbox",
                    checked="true",
                    onChange="filter_table(this)",
                    name="filter_checkbox",
                    class_="filter",
                    hidden="true",
                    **checkbox_kwargs,
                )

            def generate_summary_item(self):
                self.summary_item = html.span(
                    f"{self.total} {self.label}", class_=self.class_html
                )

        outcomes = [
            Outcome("passed", self.passed, label="通过"),
            Outcome("skipped", self.skipped, label="跳过"),
            Outcome("failed", self.failed, label="失败"),
            Outcome("error", self.errors, label="故障"),
            Outcome("xfailed", self.xfailed, label="预期的失败"),
            Outcome("xpassed", self.xpassed, label="未知的通过"),
        ]

        if self.rerun is not None:
            outcomes.append(Outcome("rerun", self.rerun, label="重跑"))

        summary = [
            html.p(
                "(取消)勾选复选框, 以便筛选测试结果.",
                class_="filter",
                hidden="true",
            ),
        ]

        for i, outcome in enumerate(outcomes, start=1):
            summary.append(outcome.checkbox)
            summary.append(outcome.summary_item)
            if i < len(outcomes):
                summary.append(", ")

        cells = [
            html.th("测试用例", class_="sortable initial-sort", col="name"),
            html.th("运行时间", class_="sortable numeric", col="duration"),
            html.th("运行结果", class_="sortable result", col="result"),
            html.th("日志资源"),
        ]
        session.config.hook.pytest_html_results_table_header(cells=cells)

        results = [
            html.table(
                [
                    html.thead(
                        html.tr(cells),
                        html.tr(
                            [
                                html.th(
                                    "无测试结果, 请考虑更换其他测试结果筛选条件.",
                                    colspan=len(cells),
                                )
                            ],
                            id="not-found-message",
                            hidden="true",
                        ),
                        id="results-table-head",
                    ),
                    self.test_logs,
                ],
                id="results-table",
            ),
        ]

        session.config.hook.pytest_html_report_title(report=self)

        self.style_css = pkg_resources.resource_string(
            __name__, os.path.join("resources", "style.css")
        ).decode("utf-8")

        main_js = pkg_resources.resource_string(
            __name__, os.path.join("resources", "main.js")
        ).decode("utf-8")

        if ANSI:
            ansi_css = [
                "\n/******************************",
                " * ANSI2HTML STYLES",
                " ******************************/\n",
            ]
            ansi_css.extend([str(r) for r in style.get_styles()])
            self.style_css += "\n".join(ansi_css)

        # <DF> Add user-provided CSS
        for path in self.config.getoption("css"):
            self.style_css += "\n/******************************"
            self.style_css += "\n * CUSTOM CSS"
            self.style_css += f"\n * {path}"
            self.style_css += "\n ******************************/\n\n"
            with open(path, "r") as f:
                self.style_css += f.read()

        css_href = "assets/style.css"
        icon_href = "assets/favicon.png"
        html_icon = html.link(href=icon_href, rel="shortcut icon")
        html_css = html.link(href=css_href, rel="stylesheet", type="text/css")
        if self.self_contained:
            html_css = html.style(raw(self.style_css))

        head = html.head(
            html.meta(charset="utf-8"), html.title(self.title), html_css
        )

        body = html.body(
            html.script(raw(main_js)),
            onLoad="init()",
        )

        overview = []
        if hasattr(self, "tester"):
            overview.append(html.p(html.strong("测试人员："), self.tester))
        if hasattr(self, "department"):
            overview.append(html.p(html.strong("测试中心："), self.department))
        overview.append(html.p(html.strong("用例统计："),
                               f"合计 {numtests} 条用例, ",
                               f"运行时间为: {suite_time_delta:.2f} 秒, ",
                               "生成时间为: {} {}".format(generated.strftime("%Y-%m-%d"), generated.strftime("%H:%M:%S"))))
        if hasattr(self, "description"):
            overview.append(html.p(html.strong("测试描述："), self.description))

        overview_html = html.div(overview, id="overview")
        body.extend([html.h1(self.title), html.h3(html.a(self.company['name'], href=self.company['url'])), overview_html])
        body.extend(self._generate_environment(session.config))

        summary_prefix, summary_postfix = [], []
        session.config.hook.pytest_html_results_summary(
            prefix=summary_prefix, summary=summary, postfix=summary_postfix
        )
        body.extend([html.h2("测试结果详情")] + summary_prefix + summary + summary_postfix)

        body.extend(results)

         # Mail Template
        MailResult.title = self.title
        MailResult.count = numtests
        MailResult.passed = self.passed
        MailResult.failed = self.failed
        MailResult.xfailed = self.xfailed
        MailResult.xpassed = self.xpassed
        MailResult.errors = self.errors
        MailResult.skipped = self.skipped

        doc = html.html(head, body)
        unicode_doc = "<!DOCTYPE html>\n{}".format(doc.unicode(indent=2))
        # Fix encoding issues, e.g. with surrogates
        unicode_doc = unicode_doc.encode("utf-8", errors="xmlcharrefreplace")
        return unicode_doc.decode("utf-8")

    def _generate_environment(self, config):
        if not hasattr(config, "_metadata") or config._metadata is None:
            return []

        metadata = config._metadata
        environment = [html.h2("测试环境")]
        rows = []

        keys = [k for k in metadata.keys()]
        # if not isinstance(metadata, OrderedDict):
        #     keys.sort()

        for key in keys:
            value = metadata[key]
            if isinstance(value, str) and value.startswith("http"):
                value = html.a(value, href=value, target="_blank")
            elif isinstance(value, (list, tuple, set)):
                value = ", ".join(str(i) for i in sorted(map(str, value)))
            elif isinstance(value, dict):
                sorted_dict = {k: value[k] for k in sorted(value)}
                value = json.dumps(sorted_dict)
            raw_value_string = raw(str(value))
            rows.append(html.tr(html.td(TEST_ENV.get(key)), html.td(raw_value_string)))

        environment.append(html.table(rows, id="environment"))
        return environment

    def _save_report(self, report_content):
        dir_name = os.path.dirname(self.report_file)
        assets_dir = os.path.join(dir_name, "assets")

        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        if not self.self_contained and not os.path.exists(assets_dir):
            os.makedirs(assets_dir)

        with open(self.report_file, "w", encoding="utf-8") as f:
            f.write(report_content)
        if not self.self_contained:
            style_path = os.path.join(assets_dir, "style.css")
            with open(style_path, "w", encoding="utf-8") as f:
                f.write(self.style_css)

    def pytest_runtest_logreport(self, report):
        if report.passed:
            self.append_passed(report)
        elif report.failed:
            self.append_failed(report)
        elif report.skipped:
            self.append_skipped(report)
        else:
            self.append_other(report)

    def pytest_collectreport(self, report):
        if report.failed:
            self.append_failed(report)

    def pytest_sessionstart(self, session):
        self.suite_start_time = time.time()

    def pytest_sessionfinish(self, session):
        report_content = self._generate_report(session)
        self._save_report(report_content)
        if session.is_email:
            smtp = SMTP(**session.email_cfg)
            smtp.sender(**session.send_cfg, attachments=self.report_file)

    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.write_sep("-", f"测试已全部完成，可打开 {self.report_file} 查看报告")


class SMTP(PATH):
    """
    Mail function based on SMTP protocol
    """

    def __init__(self, user, password, host, port=None):
        self.user = user
        self.password = password
        self.host = host
        self.port = str(port) if port is not None else "465"

    def output_email(self):
        def render_template(params: dict, template: str):
            for name, value in params.items():
                name = '${' + name + '}'
                template = template.replace(name, value)
            return template

        resources_path = self.mail_tmp_path
        render_params = {
            'mail_title': str(MailResult.title),
            'mail_count': str(MailResult.count),
            'mail_passed': str(MailResult.passed),
            'mail_failed': str(MailResult.failed),
            'mail_xfailed': str(MailResult.xfailed),
            'mail_xpassed': str(MailResult.xpassed),
            'mail_errors': str(MailResult.errors),
            'mail_skipped': str(MailResult.skipped),
            'mail_rate': format(MailResult.passed / MailResult.count * 100, '.1f')
        }

        with open(resources_path, 'rb') as file:
            body = file.read().decode('utf-8')
            contents = render_template(render_params, body)
            return contents

    def sender(self, to=None, cc=None, subject=None, contents=None, attachments=None):
        """
            邮件发送.
        :param to: 收件人，多个收件人使用列表
        :param cc: 抄送人，多个抄送人使用列表
        :param subject: 邮件标题，未提供则使用默认邮件主题
        :param contents: 邮件正文，未提供则使用默认邮件模板
        :param attachments: 邮件附件
        """
        if to is None:
            raise ValueError("Please specify the email address to send")

        if subject is None:
            subject = str(MailResult.title)
        if contents is None:
            contents = self.output_email()

        msg = MIMEMultipart()
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = self.user
        if isinstance(to, str):
            msg['To'] = to
            recipients = [to]
        elif isinstance(to, list):
            msg['To'] = ','.join(to)
            recipients = to

        if isinstance(cc, str):
            msg['Cc'] = cc
            recipients.append(cc)
        elif isinstance(cc, list):
            msg['Cc'] = ','.join(cc)
            recipients += cc

        text = MIMEText(contents, 'html', 'utf-8')
        msg.attach(text)

        if attachments is not None:
            att_name = "report.html"
            if "\\" in attachments:
                att_name = attachments.split("\\")[-1]
            if "/" in attachments:
                att_name = attachments.split("/")[-1]

            att = MIMEApplication(open(attachments, 'rb').read())
            att['Content-Type'] = 'application/octet-stream'
            att['Content-Disposition'] = 'attachment; filename="{}"'.format(att_name)
            msg.attach(att)
        try:
            smtp = smtplib.SMTP_SSL(self.host, self.port)
            smtp.login(self.user, self.password)
            smtp.sendmail(self.user, recipients, msg.as_string())
            print('📧 Email sent successfully!!')
        except BaseException as msg:
            print('❌ Email failed to send!!' + str(msg))
        finally:
            smtp.quit()
