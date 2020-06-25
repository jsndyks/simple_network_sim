import argparse
import copy
import logging
import logging.config
import sys
import time

import pandas as pd
from pathlib import Path
from typing import Optional, List

from . import data
from . import network_of_populations as ss, loaders

# Default logger, used if module not called as __main__
logger = logging.getLogger(__name__)


def main(argv):
    t0 = time.time()

    args = build_args(argv)
    setup_logger(args)
    logger.info("Parameters\n%s", "\n".join(f"\t{key}={value}" for key, value in args._get_kwargs()))

    with data.Datastore(args.data_pipeline_config) as store:
        network = ss.createNetworkOfPopulation(
            store.read_table("human/compartment-transition"),
            store.read_table("human/population"),
            store.read_table("human/commutes"),
            store.read_table("human/mixing-matrix"),
            store.read_table("human/infectious-compartments"),
            store.read_table("human/infection-probability"),
            store.read_table("human/movement-multipliers") if args.use_movement_multipliers else None,
        )

        initialInfections = []
        if args.cmd == "seeded":
            initialInfections.append(
                loaders.readInitialInfections(store.read_table("human/initial-infections"))
            )
        elif args.cmd == "random":
            for _ in range(args.trials):
                initialInfections.append(ss.randomlyInfectRegions(network, args.regions, args.age_groups, args.infected))

        results = runSimulation(network, args.time, args.trials, initialInfections)

        logger.info("Writing output")
        store.write_table("output/simple_network_sim/outbreak-timeseries", results)

        logger.info("Took %.2fs to run the simulation.", time.time() - t0)
        logger.info(
            "Use `python -m simple_network_sim.network_of_populations.visualisation -h` to find out how to take "
            "a peak what you just ran. You will need use the access-<hash>.yaml file that was created by this run."
        )


def runSimulation(
    network: ss.NetworkOfPopulation,
    max_time: int,
    trials: int,
    initialInfections: List,
) -> pd.DataFrame:
    """Run pre-created network

    :param network: object representing the network of populations
    :type network: A NetworkOfPopulation object
    :param max_time: Maximum time for simulation
    :type max_time: int
    :param trials: Number of simulation trials
    :type trials: int
    :param initialInfections: List of initial infection. If seeded, only one
    :type initialInfections: list
    :return: Averaged number of infection through time, through trials
    :rtype: list
    """
    if trials <= 1:
        # The averaging logic is slow and wastes a bunch of memory, skip it if we don't need it
        logger.info("Running simulation (1/1)")
        disposableNetwork = copy.deepcopy(network)
        ss.exposeRegions(initialInfections[0], disposableNetwork.initialState)
        return ss.basicSimulationInternalAgeStructure(disposableNetwork, max_time)
    else:
        aggregated = None

        for i in range(trials):
            logger.info("Running simulation (%s/%s)", i + 1, trials)
            disposableNetwork = copy.deepcopy(network)

            ss.exposeRegions(initialInfections[i], disposableNetwork.initialState)
            indexed = ss.basicSimulationInternalAgeStructure(disposableNetwork, max_time).set_index(
                ["time", "node", "age", "state"]
            )

            if aggregated is None:
                aggregated = indexed
            else:
                aggregated.total += indexed.total

        averaged = aggregated.reset_index()
        averaged.total /= trials

        return averaged



def setup_logger(args: Optional[argparse.Namespace] = None) -> None:
    """Configure package-level logger instance.
    
    :param args: argparse.Namespace
        args.logfile (pathlib.Path) is used to create a logfile if present
        args.quiet and args.debug control logging level to sys.stderr

    This function can be called without args, in which case it configures the
    package logger to write WARNING and above to STDERR.

    When called with args, it uses args.logfile to determine if logs (by
    default, INFO and above) should be written to a file, and the path of
    that file. args.quiet and args.debug are used to control reporting
    level.
    """
    # Dictionary to define logging configuration
    logconf = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
            },
        },
        "handlers": {
            "stderr": {
                "level": "INFO",
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {__package__: {"handlers": ["stderr"], "level": "DEBUG"}},
    }

    # If args.logpath is specified, add logfile
    if args is not None and args.logfile is not None:
        logdir = args.logfile.parents[0]
        # If the logfile is going in another directory, we must
        # create/check if the directory is there
        try:
            if not logdir == Path.cwd():
                logdir.mkdir(exist_ok=True)
        except OSError:
            logger.error("Could not create %s for logging", logdir, exc_info=True)
            raise SystemExit(1)  # Substitute meaningful error code when known
        # Add logfile configuration
        logconf["handlers"]["logfile"] = {
            "class": "logging.FileHandler",
            "level": "INFO",
            "formatter": "standard",
            "filename": str(args.logfile),
            "encoding": "utf8",
        }
        logconf["loggers"][__package__]["handlers"].append("logfile")

    # Set STDERR/logfile levels if args.quiet/args.debug specified
    if args is not None and args.quiet:
        logconf["handlers"]["stderr"]["level"] = "WARNING"
    elif args is not None and args.debug:
        logconf["handlers"]["stderr"]["level"] = "DEBUG"
        if "logfile" in logconf["handlers"]:
            logconf["handlers"]["logfile"]["level"] = "DEBUG"

    # Configure logger
    logging.config.dictConfig(logconf)


def build_args(argv):
    """Return parsed CLI arguments as argparse.Namespace.

    :param argv: CLI arguments
    :type argv: list
    """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Uses the deterministic network of populations model to simulation the disease progression",
    )
    parser.add_argument(
        "--use-movement-multipliers",
        action="store_true",
        help="By enabling this parameter you can adjust dampening or heightening people movement through time",
    )
    parser.add_argument(
        "--time",
        default=200,
        type=int,
        help="The number of time steps to take for each simulation",
    )
    parser.add_argument(
        "-l",
        "--logfile",
        dest="logfile",
        default=None,
        type=Path,
        help="Path for logging output",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        dest="quiet",
        action="store_true",
        help="Prints only warnings to stderr",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Provide debug output to STDERR"
    )
    parser.add_argument(
        "-c",
        "--data-pipeline-config",
        default="config.yaml",
        help="Base directory with the input paramters",
    )

    sp = parser.add_subparsers(dest="cmd", required=True)

    # Parameters when using the random infection approach
    randomCmd = sp.add_parser(
        "random",
        help="Randomly pick regions to infect",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    randomCmd.add_argument("--regions", default=1, help="Number of regions to infect")
    randomCmd.add_argument(
        "--age-groups", nargs="+", default=["[17,70)"], help="Age groups to infect"
    )
    randomCmd.add_argument(
        "--trials", default=100, type=int, help="Number of experiments to run"
    )
    randomCmd.add_argument(
        "--infected",
        default=100,
        type=int,
        help="Number of infected people in each region/age group",
    )

    # Parameters when using the seeded infection approach
    seededCmd = sp.add_parser(
        "seeded",
        help="Use a seed file with infected regions",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    seededCmd.add_argument(
        "--trials", default=1, type=int, help="Number of experiments to run"
    )

    return parser.parse_args(argv)


if __name__ == "__main__":
    # The logger name inherits from the package, if called as __main__
    logger = logging.getLogger(f"{__package__}.{__name__}")
    main(sys.argv[1:])
