#!/usr/bin/python
#
# ==-- process-stats-dir - summarize one or more Swift -stats-output-dirs --==#
#
# This source file is part of the Swift.org open source project
#
# Copyright (c) 2014-2017 Apple Inc. and the Swift project authors
# Licensed under Apache License v2.0 with Runtime Library Exception
#
# See https://swift.org/LICENSE.txt for license information
# See https://swift.org/CONTRIBUTORS.txt for the list of Swift project authors
#
# ==------------------------------------------------------------------------==#
#
# This file processes the contents of one or more directories generated by
# `swiftc -stats-output-dir` and emits summary data, traces etc. for analysis.

import argparse
import csv
import datetime
import json
import os
import platform
import random
import re
import sys
import time
import urllib
import urllib2


class JobStats:

    def __init__(self, jobkind, jobid, module, start_usec, dur_usec,
                 jobargs, stats):
        self.jobkind = jobkind
        self.jobid = jobid
        self.module = module
        self.start_usec = start_usec
        self.dur_usec = dur_usec
        self.jobargs = jobargs
        self.stats = stats

    def is_driver_job(self):
        return self.jobkind == 'driver'

    def is_frontend_job(self):
        return self.jobkind == 'frontend'

    def driver_jobs_ran(self):
        assert(self.is_driver_job())
        return self.stats.get("Driver.NumDriverJobsRun", 0)

    def driver_jobs_skipped(self):
        assert(self.is_driver_job())
        return self.stats.get("Driver.NumDriverJobsSkipped", 0)

    def driver_jobs_total(self):
        assert(self.is_driver_job())
        return self.driver_jobs_ran() + self.driver_jobs_skipped()

    def merged_with(self, other):
        merged_stats = {}
        for k, v in self.stats.items() + other.stats.items():
            merged_stats[k] = v + merged_stats.get(k, 0.0)
        merged_kind = self.jobkind
        if other.jobkind != merged_kind:
            merged_kind = "<merged>"
        merged_module = self.module
        if other.module != merged_module:
            merged_module = "<merged>"
        merged_start = min(self.start_usec, other.start_usec)
        merged_end = max(self.start_usec + self.dur_usec,
                         other.start_usec + other.dur_usec)
        merged_dur = merged_end - merged_start
        return JobStats(merged_kind, random.randint(0, 1000000000),
                        merged_module, merged_start, merged_dur,
                        self.jobargs + other.jobargs, merged_stats)

    def incrementality_percentage(self):
        assert(self.is_driver_job())
        ran = self.driver_jobs_ran()
        total = self.driver_jobs_total()
        return round((float(ran) / float(total)) * 100.0, 2)

    # Return a JSON-formattable object of the form preferred by google chrome's
    # 'catapult' trace-viewer.
    def to_catapult_trace_obj(self):
        return {"name": self.module,
                "cat": self.jobkind,
                "ph": "X",              # "X" == "complete event"
                "pid": self.jobid,
                "tid": 1,
                "ts": self.start_usec,
                "dur": self.dur_usec,
                "args": self.jobargs}

    def start_timestr(self):
        t = datetime.datetime.fromtimestamp(self.start_usec / 1000000.0)
        return t.strftime("%Y-%m-%d %H:%M:%S")

    def end_timestr(self):
        t = datetime.datetime.fromtimestamp((self.start_usec +
                                             self.dur_usec) / 1000000.0)
        return t.strftime("%Y-%m-%d %H:%M:%S")

    def pick_lnt_metric_suffix(self, metric_name):
        if "BytesOutput" in metric_name:
            return "code_size"
        if "RSS" in metric_name or "BytesAllocated" in metric_name:
            return "mem"
        return "compile"

    # Return a JSON-formattable object of the form preferred by LNT's
    # 'submit' format.
    def to_lnt_test_obj(self, args):
        run_info = {
            "run_order": str(args.lnt_order),
            "tag": str(args.lnt_tag),
        }
        run_info.update(dict(args.lnt_run_info))
        stats = self.stats
        return {
            "Machine":
            {
                "Name": args.lnt_machine,
                "Info": dict(args.lnt_machine_info)
            },
            "Run":
            {
                "Start Time": self.start_timestr(),
                "End Time": self.end_timestr(),
                "Info": run_info
            },
            "Tests":
            [
                {
                    "Data": [v],
                    "Info": {},
                    "Name": "%s.%s.%s.%s" % (args.lnt_tag, self.module,
                                             k, self.pick_lnt_metric_suffix(k))
                }
                for (k, v) in stats.items()
            ]
        }


# Return an array of JobStats objects
def load_stats_dir(path):
    jobstats = []
    auxpat = (r"(?P<module>[^-]+)-(?P<input>[^-]+)-(?P<triple>[^-]+)" +
              r"-(?P<out>[^-]+)-(?P<opt>[^-]+)")
    fpat = (r"^stats-(?P<start>\d+)-swift-(?P<kind>\w+)-" +
            auxpat +
            r"-(?P<pid>\d+)(-.*)?.json$")
    for root, dirs, files in os.walk(path):
        for f in files:
            m = re.match(fpat, f)
            if m:
                # NB: "pid" in fpat is a random number, not unix pid.
                mg = m.groupdict()
                jobkind = mg['kind']
                jobid = int(mg['pid'])
                start_usec = int(mg['start'])
                module = mg["module"]
                jobargs = [mg["input"], mg["triple"], mg["out"], mg["opt"]]

                j = json.load(open(os.path.join(root, f)))
                dur_usec = 1
                patstr = (r"time\.swift-" + jobkind + r"\." + auxpat +
                          r"\.wall$")
                pat = re.compile(patstr)
                stats = dict()
                for (k, v) in j.items():
                    if k.startswith("time."):
                        v = int(1000000.0 * float(v))
                    stats[k] = v
                    tm = re.match(pat, k)
                    if tm:
                        dur_usec = v

                e = JobStats(jobkind=jobkind, jobid=jobid,
                             module=module, start_usec=start_usec,
                             dur_usec=dur_usec, jobargs=jobargs,
                             stats=stats)
                jobstats.append(e)
    return jobstats


# Passed args with 2-element remainder ["old", "new"], return a list of tuples
# of the form [(name, (oldstats, newstats))] where each name is a common subdir
# of each of "old" and "new", and the stats are those found in the respective
# dirs.
def load_paired_stats_dirs(args):
    assert(len(args.remainder) == 2)
    paired_stats = []
    (old, new) = args.remainder
    for p in sorted(os.listdir(old)):
        full_old = os.path.join(old, p)
        full_new = os.path.join(new, p)
        if not (os.path.exists(full_old) and os.path.isdir(full_old) and
                os.path.exists(full_new) and os.path.isdir(full_new)):
            continue
        old_stats = load_stats_dir(full_old)
        new_stats = load_stats_dir(full_new)
        if len(old_stats) == 0 or len(new_stats) == 0:
            continue
        paired_stats.append((p, (old_stats, new_stats)))
    return paired_stats


def write_catapult_trace(args):
    allstats = []
    for path in args.remainder:
        allstats += load_stats_dir(path)
    json.dump([s.to_catapult_trace_obj() for s in allstats], args.output)


def write_lnt_values(args):
    for d in args.remainder:
        stats = load_stats_dir(d)
        merged = merge_all_jobstats(stats)
        j = merged.to_lnt_test_obj(args)
        if args.lnt_submit is None:
            json.dump(j, args.output, indent=4)
        else:
            url = args.lnt_submit
            print "\nsubmitting to LNT server: " + url
            json_report = {'input_data': json.dumps(j), 'commit': '1'}
            data = urllib.urlencode(json_report)
            response_str = urllib2.urlopen(urllib2.Request(url, data))
            response = json.loads(response_str.read())
            print "### response:"
            print response
            if 'success' in response:
                print "server response:\tSuccess"
            else:
                print "server response:\tError"
                print "error:\t", response['error']
                sys.exit(1)


def merge_all_jobstats(jobstats):
    m = None
    for j in jobstats:
        if m is None:
            m = j
        else:
            m = m.merged_with(j)
    return m


def show_paired_incrementality(args):
    fieldnames = ["old_pct", "old_skip",
                  "new_pct", "new_skip",
                  "delta_pct", "delta_skip",
                  "name"]
    out = csv.DictWriter(args.output, fieldnames, dialect='excel-tab')
    out.writeheader()

    for (name, (oldstats, newstats)) in load_paired_stats_dirs(args):
        olddriver = merge_all_jobstats([x for x in oldstats
                                        if x.is_driver_job()])
        newdriver = merge_all_jobstats([x for x in newstats
                                        if x.is_driver_job()])
        if olddriver is None or newdriver is None:
            continue
        oldpct = olddriver.incrementality_percentage()
        newpct = newdriver.incrementality_percentage()
        deltapct = newpct - oldpct
        oldskip = olddriver.driver_jobs_skipped()
        newskip = newdriver.driver_jobs_skipped()
        deltaskip = newskip - oldskip
        out.writerow(dict(name=name,
                          old_pct=oldpct, old_skip=oldskip,
                          new_pct=newpct, new_skip=newskip,
                          delta_pct=deltapct, delta_skip=deltaskip))


def show_incrementality(args):
    fieldnames = ["incrementality", "name"]
    out = csv.DictWriter(args.output, fieldnames, dialect='excel-tab')
    out.writeheader()

    for path in args.remainder:
        stats = load_stats_dir(path)
        for s in stats:
            if s.is_driver_job():
                pct = s.incrementality_percentage()
                out.writerow(dict(name=os.path.basename(path),
                                  incrementality=pct))


def diff_and_pct(old, new):
    if old == 0:
        if new == 0:
            return (0, 0.0)
        else:
            return (new, 100.0)
    delta = (new - old)
    delta_pct = round((float(delta) / float(old)) * 100.0, 2)
    return (delta, delta_pct)


def update_epoch_value(d, name, epoch, value):
    changed = 0
    if name in d:
        (existing_epoch, existing_value) = d[name]
        if existing_epoch > epoch:
            print("note: keeping newer value %d from epoch %d for %s"
                  % (existing_value, existing_epoch, name))
            epoch = existing_epoch
            value = existing_value
        elif existing_value == value:
            epoch = existing_epoch
        else:
            (_, delta_pct) = diff_and_pct(existing_value, value)
            print ("note: changing value %d -> %d (%.2f%%) for %s" %
                   (existing_value, value, delta_pct, name))
            changed = 1
    d[name] = (epoch, value)
    return (epoch, value, changed)


def read_stats_dict_from_csv(f):
    infieldnames = ["epoch", "name", "value"]
    c = csv.DictReader(f, infieldnames,
                       dialect='excel-tab',
                       quoting=csv.QUOTE_NONNUMERIC)
    d = {}
    for row in c:
        epoch = int(row["epoch"])
        name = row["name"]
        value = int(row["value"])
        update_epoch_value(d, name, epoch, value)
    return d


# The idea here is that a "baseline" is a (tab-separated) CSV file full of
# the counters you want to track, each prefixed by an epoch timestamp of
# the last time the value was reset.
#
# When you set a fresh baseline, all stats in the provided stats dir are
# written to the baseline. When you set against an _existing_ baseline,
# only the counters mentioned in the existing baseline are updated, and
# only if their values differ.
#
# Finally, since it's a line-oriented CSV file, you can put:
#
#    mybaseline.csv merge=union
#
# in your .gitattributes file, and forget about merge conflicts. The reader
# function above will take the later epoch anytime it detects duplicates,
# so union-merging is harmless. Duplicates will be eliminated whenever the
# next baseline-set is done.
def set_csv_baseline(args):
    existing = None
    if os.path.exists(args.set_csv_baseline):
        with open(args.set_csv_baseline, "r") as f:
            existing = read_stats_dict_from_csv(f)
            print ("updating %d baseline entries in %s" %
                   (len(existing), args.set_csv_baseline))
    else:
        print "making new baseline " + args.set_csv_baseline
    fieldnames = ["epoch", "name", "value"]
    with open(args.set_csv_baseline, "wb") as f:
        out = csv.DictWriter(f, fieldnames, dialect='excel-tab',
                             quoting=csv.QUOTE_NONNUMERIC)
        m = merge_all_jobstats([s for d in args.remainder
                                for s in load_stats_dir(d)])
        changed = 0
        newepoch = int(time.time())
        for name in sorted(m.stats.keys()):
            epoch = newepoch
            value = m.stats[name]
            if existing is not None:
                if name not in existing:
                    continue
                (epoch, value, chg) = update_epoch_value(existing, name,
                                                         epoch, value)
                changed += chg
            out.writerow(dict(epoch=int(epoch),
                              name=name,
                              value=int(value)))
        if existing is not None:
            print "changed %d entries in baseline" % changed
    return 0


def compare_to_csv_baseline(args):
    old_stats = read_stats_dict_from_csv(args.compare_to_csv_baseline)
    m = merge_all_jobstats([s for d in args.remainder
                            for s in load_stats_dir(d)])
    new_stats = m.stats

    regressions = 0
    outfieldnames = ["old", "new", "delta_pct", "name"]
    out = csv.DictWriter(args.output, outfieldnames, dialect='excel-tab')
    out.writeheader()

    for stat_name in sorted(old_stats.keys()):
        (_, old) = old_stats[stat_name]
        new = new_stats.get(stat_name, 0)
        (delta, delta_pct) = diff_and_pct(old, new)
        if (stat_name.startswith("time.") and
           abs(delta) < args.delta_usec_thresh):
            continue
        if abs(delta_pct) < args.delta_pct_thresh:
            continue
        out.writerow(dict(name=stat_name,
                          old=int(old), new=int(new),
                          delta_pct=delta_pct))
        if delta > 0:
            regressions += 1
    return regressions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true",
                        help="Report activity verbosely")
    parser.add_argument("--output", default="-",
                        type=argparse.FileType('wb', 0),
                        help="Write output to file")
    parser.add_argument("--paired", action="store_true",
                        help="Process two dirs-of-stats-dirs, pairwise")
    parser.add_argument("--delta-pct-thresh", type=float, default=0.01,
                        help="Percentage change required to report")
    parser.add_argument("--delta-usec-thresh", type=int, default=100000,
                        help="Absolute delta on times required to report")
    parser.add_argument("--lnt-machine", type=str, default=platform.node(),
                        help="Machine name for LNT submission")
    parser.add_argument("--lnt-run-info", action='append', default=[],
                        type=lambda kv: kv.split("="),
                        help="Extra key=value pairs for LNT run-info")
    parser.add_argument("--lnt-machine-info", action='append', default=[],
                        type=lambda kv: kv.split("="),
                        help="Extra key=value pairs for LNT machine-info")
    parser.add_argument("--lnt-order", type=str,
                        default=str(int(time.time())),
                        help="Order for LNT submission")
    parser.add_argument("--lnt-tag", type=str, default="swift-compile",
                        help="Tag for LNT submission")
    parser.add_argument("--lnt-submit", type=str, default=None,
                        help="URL to submit LNT data to (rather than print)")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--catapult", action="store_true",
                       help="emit a 'catapult'-compatible trace of events")
    modes.add_argument("--incrementality", action="store_true",
                       help="summarize the 'incrementality' of a build")
    modes.add_argument("--set-csv-baseline", type=str, default=None,
                       help="Merge stats from a stats-dir into a CSV baseline")
    modes.add_argument("--compare-to-csv-baseline",
                       type=argparse.FileType('rb', 0), default=None,
                       metavar="BASELINE.csv",
                       help="Compare stats dir to named CSV baseline")
    modes.add_argument("--lnt", action="store_true",
                       help="Emit an LNT-compatible test summary")
    parser.add_argument('remainder', nargs=argparse.REMAINDER,
                        help="stats-dirs to process")

    args = parser.parse_args()
    if len(args.remainder) == 0:
        parser.print_help()
        return 1
    if args.catapult:
        write_catapult_trace(args)
    elif args.set_csv_baseline is not None:
        return set_csv_baseline(args)
    elif args.compare_to_csv_baseline is not None:
        return compare_to_csv_baseline(args)
    elif args.incrementality:
        if args.paired:
            show_paired_incrementality(args)
        else:
            show_incrementality(args)
    elif args.lnt:
        write_lnt_values(args)
    return None


sys.exit(main())
