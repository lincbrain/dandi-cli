from datetime import datetime
import json
import os
from pathlib import Path
from subprocess import check_output, run
import shutil
import tempfile

from dateutil.tz import tzlocal, tzutc
import numpy as np
import pynwb
import pynwb.ecephys
import pytest
import requests

from .skip import skipif
from .. import get_logger
from ..cli.command import organize
from ..consts import dandiset_metadata_file, known_instances
from ..pynwb_utils import make_nwb_file, metadata_nwb_file_fields
from ..register import register
from ..upload import upload
from ..utils import yaml_load


lgr = get_logger()


# TODO: move into some common fixtures.  We might produce a number of files
#       and also carry some small ones directly in git for regression testing
@pytest.fixture(scope="session")
def simple1_nwb_metadata(tmpdir_factory):
    # very simple assignment with the same values as the key with 1 as suffix
    metadata = {f: "{}1".format(f) for f in metadata_nwb_file_fields}
    # subject_fields

    # tune specific ones:
    # Needs an explicit time zone since otherwise pynwb would add one
    # But then comparison breaks anyways any ways yoh have tried to set it
    # for datetime.now.  Taking example from pynwb tests
    metadata["session_start_time"] = datetime(2017, 4, 15, 12, tzinfo=tzutc())
    metadata["keywords"] = ["keyword1", "keyword 2"]
    # since NWB 2.1.0 some fields values become "arrays" which are
    # then reloaded as tuples, so we force them being tuples here
    # See e.g. https://github.com/NeurodataWithoutBorders/pynwb/issues/1091
    for f in "related_publications", "experimenter":
        metadata[f] = (metadata[f],)
    return metadata


@pytest.fixture(scope="session")
def simple1_nwb(simple1_nwb_metadata, tmpdir_factory):
    return make_nwb_file(
        str(tmpdir_factory.mktemp("data").join("simple1.nwb")), **simple1_nwb_metadata
    )


@pytest.fixture(scope="session")
def simple2_nwb(simple1_nwb_metadata, tmpdir_factory):
    """With a subject"""
    return make_nwb_file(
        str(tmpdir_factory.mktemp("data").join("simple2.nwb")),
        subject=pynwb.file.Subject(
            subject_id="mouse001",
            date_of_birth=datetime(2019, 12, 1, tzinfo=tzutc()),
            sex="M",
            species="mouse",
        ),
        **simple1_nwb_metadata,
    )


@pytest.fixture(scope="session")
def organized_nwb_dir(simple2_nwb, tmp_path_factory, clirunner):
    tmp_path = tmp_path_factory.mktemp("dandiset")
    (tmp_path / dandiset_metadata_file).write_text("{}\n")
    r = clirunner.invoke(
        organize, ["-f", "copy", "--dandiset-path", str(tmp_path), str(simple2_nwb)]
    )
    assert r.exit_code == 0, r.stdout
    return tmp_path


@pytest.fixture(scope="session")
def organized_nwb_dir2(simple1_nwb_metadata, simple2_nwb, tmp_path_factory, clirunner):
    tmp_path = tmp_path_factory.mktemp("dandiset")

    # need to copy first and then use -f move since we will create one more
    # file to be "organized"
    shutil.copy(str(simple2_nwb), str(tmp_path))
    make_nwb_file(
        str(tmp_path / "simple3.nwb"),
        subject=pynwb.file.Subject(
            subject_id="lizard001",
            date_of_birth=datetime(2019, 12, 1, tzinfo=tzutc()),
            sex="F",
            species="Gekko gecko",
        ),
        **simple1_nwb_metadata,
    )
    (tmp_path / dandiset_metadata_file).write_text("{}\n")
    r = clirunner.invoke(organize, ["-f", "move", "--dandiset-path", str(tmp_path)])
    assert r.exit_code == 0, r.stdout
    assert sum(p.is_dir() for p in tmp_path.iterdir()) == 2
    return tmp_path


@pytest.fixture(scope="session")
def large_nwb(tmpdir_factory):
    nwbfile = pynwb.NWBFile(
        "Randomly generated data",
        "RANDOM",
        datetime.now(tzlocal()),
        subject=pynwb.file.Subject(
            subject_id="mouse001",
            date_of_birth=datetime(2019, 12, 1, tzinfo=tzutc()),
            sex="M",
            species="mouse",
        ),
    )
    device = nwbfile.create_device(name="acme123")
    electrode_group = nwbfile.create_electrode_group(
        "tetrode1",
        description="A sample tetrode",
        location="pineal gland",
        device=device,
    )
    nwbfile.add_electrode(
        id=0,
        x=1.0,
        y=2.0,
        z=3.0,
        imp=0.0,
        location="CA1",
        filtering="none",
        group=electrode_group,
    )
    electrode_table_region = nwbfile.create_electrode_table_region(
        [0], "the first electrode"
    )
    rate = 10.0
    data_len = 65536 * 64
    data = np.random.rand(data_len * 2).reshape((data_len, 2))
    ephys_ts = pynwb.ecephys.ElectricalSeries(
        "test_ephys_data",
        data,
        electrode_table_region,
        starting_time=0.0,
        rate=rate,
        resolution=0.001,
    )
    nwbfile.add_acquisition(ephys_ts)
    path = str(tmpdir_factory.mktemp("data").join("large.nwb"))
    with pynwb.NWBHDF5IO(path, "w") as io:
        io.write(nwbfile)
    return path


@pytest.fixture(scope="session")
def large_dandiset(clirunner, large_nwb, local_docker_compose, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("dandiset")
    (tmp_path / dandiset_metadata_file).write_text("{}\n")
    r = clirunner.invoke(
        organize, ["-f", "copy", "--dandiset-path", str(tmp_path), str(large_nwb)]
    )
    assert r.exit_code == 0, r.stdout
    dandi_instance_id = local_docker_compose["instance_id"]
    # monkeypatch can't be used here because it's function-scoped.
    old_api_key = os.environ.get("DANDI_API_KEY")
    os.environ["DANDI_API_KEY"] = local_docker_compose["api_key"]
    try:
        register(
            "Large Dandiset",
            "Dandiset with a large NWB file",
            dandiset_path=tmp_path,
            dandi_instance=dandi_instance_id,
        )
        with (tmp_path / dandiset_metadata_file).open() as fp:
            metadata = yaml_load(fp, typ="safe")
        dandi_id = metadata["identifier"]
        upload(
            paths=[tmp_path],
            dandiset_path=tmp_path,
            dandi_instance=dandi_instance_id,
            devel_debug=True,
        )
    finally:
        if old_api_key is None:
            del os.environ["DANDI_API_KEY"]
        else:
            os.environ["DANDI_API_KEY"] = old_api_key
    return f"{local_docker_compose['instance'].redirector}/dandiset/{dandi_id}/draft"


@pytest.fixture(scope="session")
def clirunner():
    """A shortcut to get a click.runner to run a command"""
    from click.testing import CliRunner

    yield CliRunner()


def get_gitrepo_fixture(url, commitish=None, scope="session"):

    if commitish:
        raise NotImplementedError()

    @pytest.fixture(scope=scope)
    def fixture():
        # TODO: adapt reproman.tests.skip collection of skipif conditions
        # skipif.no_network()
        # skipif.no_git()

        path = tempfile.mktemp()  # not using pytest's tmpdir fixture to not
        # collide in different scopes etc. But we
        # would need to remove it ourselves
        lgr.debug("Cloning %r into %r", url, path)
        try:
            runout = run(["git", "clone", url, path])
            if runout.returncode:
                raise RuntimeError(f"Failed to clone {url} into {path}")
            yield path
        finally:
            try:
                shutil.rmtree(path)
            except BaseException as exc:
                lgr.warning("Failed to remove %s - using Windows?: %s", path, exc)

    return fixture


nwb_test_data = get_gitrepo_fixture("http://github.com/dandi-datasets/nwb_test_data")


LOCAL_DOCKER_DIR = Path(__file__).with_name("data") / "dandiarchive-docker"
LOCAL_DOCKER_ENV = LOCAL_DOCKER_DIR.name


@pytest.fixture(scope="session")
def local_docker_compose():
    instance_id = "local-docker-tests"
    instance = known_instances[instance_id]

    # if api_key is specified, we are reusing some already running instance
    # so we would not bother starting/stopping a new one here
    api_key = os.environ.get("DANDI_REUSE_LOCAL_DOCKER_TESTS_API_KEY")
    if api_key:
        yield {"api_key": api_key, "instance": instance, "instance_id": instance_id}
        return

    skipif.no_network()
    skipif.no_docker_engine()

    # Check that we're running on a Unix-based system (Linux or macOS), as the
    # Docker images don't work on Windows.
    if os.name != "posix":
        pytest.skip("Docker images require Unix host")

    run(
        ["docker-compose", "up", "-d", "provision"],
        cwd=str(LOCAL_DOCKER_DIR),
        check=True,
    )
    try:
        run(["docker", "wait", f"{LOCAL_DOCKER_ENV}_provision_1"], check=True)
        # Should we check that the output of `docker wait` is 0?

        r = requests.get(
            f"{instance.girder}/api/v1/user/authentication", auth=("admin", "letmein")
        )
        r.raise_for_status()
        initial_api_key = r.json()["authToken"]["token"]

        # Get an unscoped/full permissions API key that can be used for
        # uploading:
        r = requests.post(
            f"{instance.girder}/api/v1/api_key",
            params={"name": "testkey", "tokenDuration": 1},
            headers={"Girder-Token": initial_api_key},
        )
        r.raise_for_status()
        api_key = r.json()["key"]

        # dandi-publish requires an admin named "publish":
        requests.post(
            f"{instance.girder}/api/v1/user",
            data={
                "login": "publish",
                "email": "nil@nil.nil",
                "firstName": "Dandi",
                "lastName": "Publish",
                "password": "Z1lT4Fh7Kj",
                "admin": "True",
            },
            headers={"Girder-Token": initial_api_key},
        ).raise_for_status()

        r = requests.get(
            f"{instance.girder}/api/v1/user/authentication",
            auth=("publish", "Z1lT4Fh7Kj"),
        )
        r.raise_for_status()
        publish_api_key = r.json()["authToken"]["token"]

        # We can't refer to the Girder container as "girder" in
        # DJANGO_DANDI_GIRDER_API_URL because Django doesn't recognize
        # single-component domain names as valid.  Hence, we need to instead
        # use the Girder container's in-network IP address.
        about_girder = check_output(
            ["docker", "inspect", f"{LOCAL_DOCKER_ENV}_girder_1"]
        )
        # TODO: Figure out how to do this all with `docker inspect`'s "-f"
        # argument (Note that the hyphen in LOCAL_DOCKER_ENV makes it tricky):
        girder_ip = json.loads(about_girder)[0]["NetworkSettings"]["Networks"][
            f"{LOCAL_DOCKER_ENV}_default"
        ]["IPAddress"]

        env = dict(os.environ)
        env["DJANGO_DANDI_GIRDER_API_URL"] = f"http://{girder_ip}:8080/api/v1"
        env["DJANGO_DANDI_GIRDER_API_KEY"] = publish_api_key

        run(
            ["docker-compose", "run", "--rm", "django", "./manage.py", "migrate"],
            cwd=str(LOCAL_DOCKER_DIR),
            env=env,
            check=True,
        )

        run(
            [
                "docker-compose",
                "run",
                "--rm",
                "-e",
                "DJANGO_SUPERUSER_PASSWORD=nsNc48DBiS",
                "django",
                "./manage.py",
                "createsuperuser",
                "--no-input",
                "--username",
                "admin",
                "--email",
                "nil@nil.nil",
            ],
            cwd=str(LOCAL_DOCKER_DIR),
            env=env,
            check=True,
        )

        django_api_key = check_output(
            [
                "docker-compose",
                "run",
                "--rm",
                "django",
                "./manage.py",
                "drf_create_token",
                "admin",
            ],
            cwd=str(LOCAL_DOCKER_DIR),
            env=env,
        ).split()[2]

        run(
            ["docker-compose", "up", "-d", "django", "celery"],
            cwd=str(LOCAL_DOCKER_DIR),
            env=env,
            check=True,
        )

        requests.put(
            f"{instance.girder}/api/v1/system/setting",
            data={
                "key": "dandi.publish_api_url",
                "value": "http://django:8000/api/",  # django or localhost?
            },
            headers={"Girder-Token": publish_api_key},
        ).raise_for_status()

        requests.put(
            f"{instance.girder}/api/v1/system/setting",
            data={"key": "dandi.publish_api_key", "value": django_api_key},
            headers={"Girder-Token": publish_api_key},
        ).raise_for_status()

        yield {"api_key": api_key, "instance": instance, "instance_id": instance_id}
    finally:
        run(["docker-compose", "down", "-v"], cwd=str(LOCAL_DOCKER_DIR), check=True)


@pytest.fixture()
def local_docker_compose_env(local_docker_compose, monkeypatch):
    monkeypatch.setenv("DANDI_API_KEY", local_docker_compose["api_key"])
    return local_docker_compose
