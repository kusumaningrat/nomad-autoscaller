from prometheus_api_client import PrometheusConnect
from jinja2 import Environment, FileSystemLoader
from flask import render_template
from dotenv import load_dotenv
import nomad, os, time, subprocess, json


PROM_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
NOMAD_ADDR = os.getenv("NOMAD_ADDR", "http://localhost:4646")
NAMESPACE = os.getenv("NAMESPACE", "testing")

prom = PrometheusConnect(
    url=PROM_URL,
    disable_ssl=True,
)

nomad_client = nomad.Nomad(
    host=NOMAD_ADDR,
    timeout=5,
)
registry_env = load_dotenv('.env')


def scaled_job_name(job_name):
    if job_name.endswith("-scaled"):
        return job_name
    return f"{job_name}-scaled"

def job_exists(job_name, namespace):
    try:
        nomad_client.job.get_job(job_name, namespace=namespace)
        return True
    except Exception:
        return False

def normalize_job_name(job_name):
    if job_name.endswith("-idle"):
        return job_name[:-5]  # remove "-idle"
    return job_name


def job_has_allocations(job_name, namespace):
    try:
        allocs = nomad_client.job.get_allocations(job_name, namespace=namespace)
        return bool(allocs)
    except Exception:
        return False



def stop_job(job_name, namespace):
    try:
        print(f"Stopping job: {job_name}")
        nomad_client.job.deregister_job(job_name, namespace=namespace)
    except Exception as e:
        print(f"Failed to stop job {job_name}: {e}")


def deploy_job(hcl_path):
    subprocess.run(["nomad", "run", hcl_path], check=True)

def get_job_workers(job_name, namespace):
    try:
        allocs = nomad_client.job.get_job(job_name, namespace=namespace)
        worker_name = allocs["TaskGroups"][0]["Constraints"][0]["RTarget"]
        return worker_name
    except Exception:
        return set()


def checkEligibleNode(cpu, memory, threshold=90, exclude_nodes=None):
    if exclude_nodes is None:
        exclude_nodes = set()
    elif isinstance(exclude_nodes, str):
        exclude_nodes = {exclude_nodes}
    else:
        exclude_nodes = set(exclude_nodes)

    cpu_usage = {}
    memory_usage = {}
    excluded_workers = {"Worker-05", "Worker-06"}
    excluded_workers |= exclude_nodes

    print("Excluded_workers:", excluded_workers)

    for c in cpu:
        node_name = c["metric"]["nodename"]
        cpu_value = round(float(c["value"][1]), 2)
        if node_name.startswith("Worker"):
            cpu_usage[node_name] = cpu_value

    for m in memory:
        node_name = m["metric"]["nodename"]
        memory_value = round(float(m["value"][1]), 2)
        if node_name.startswith("Worker"):
            memory_usage[node_name] = memory_value

    for node_name, cpu_value in cpu_usage.items():
        if node_name in excluded_workers:
            continue

        mem_value = memory_usage.get(node_name, 0)

        if cpu_value <= threshold and mem_value <= threshold:
            return node_name

    return None


def generateJob(
    base_job,
    new_job_name,
    namespace,
    target_worker,
    state="idle",          # idle | base
    idle_cpu=50,
    idle_memory=64,
):
    job = nomad_client.job.get_job(base_job, namespace=namespace)

    tg = job["TaskGroups"][0]
    task = tg["Tasks"][0]

    # 🔹 ORIGINAL RESOURCES
    orig_cpu = task["Resources"]["CPU"]
    orig_memory = task["Resources"]["MemoryMB"]

    # 🔹 STATE-BASED RESOURCE SELECTION
    if state == "idle":
        cpu = idle_cpu
        memory = idle_memory
    else:
        cpu = orig_cpu
        memory = orig_memory

    env = Environment(
        loader=FileSystemLoader("templates"),
        autoescape=False
    )

    context = {
        "job_name": new_job_name,
        "datacenter_name": job["Datacenters"][0],
        "namespace": namespace,
        "group_name": tg["Name"],
        "exposed_port": tg["Networks"][0]["ReservedPorts"][0]["Value"],
        "container_port": tg["Networks"][0]["ReservedPorts"][0]["To"],
        "health_check_path": tg["Services"][0]["Checks"][0]["Path"],
        "dns_servers": json.dumps(task["Config"].get("dns_servers", [])),
        "registry_url": task["Config"]["image"],
        "registry_user": os.getenv("REGISTRY_USERNAME"),
        "registry_pass": os.getenv("REGISTRY_PASSWORD"),
        "vault_role": task["Vault"]["Role"],
        "vault_keys": task["Templates"][0]["EmbeddedTmpl"],
        "cpu": cpu,
        "memory": memory,
        "new_target_worker": target_worker,
    }

    hcl = env.get_template("template.j2").render(context)

    hcl_dir = f"jobs/{context['group_name']}"
    os.makedirs(hcl_dir, exist_ok=True)

    hcl_path = f"{hcl_dir}/{new_job_name}.hcl"

    with open(hcl_path, "w") as f:
        f.write(hcl)

    print(f"Deploying job [{state.upper()}]: {new_job_name}")
    subprocess.run(["nomad", "run", hcl_path], check=True)

def run_base_job(base_job, namespace):
    hcl_path = f"jobs/{base_job}/{base_job}.hcl"

    if not os.path.exists(hcl_path):
        print(f"Base job HCL not found: {hcl_path}")
        return

    print(f"Deploying base job: {base_job}")
    subprocess.run(["nomad", "run", hcl_path], check=True)


def resourceChecker():
    mem_query = """
        max by (exported_job, task_group, namespace) (
          (
            avg_over_time(nomad_client_allocs_memory_usage[2d])
            -
            avg_over_time(nomad_client_allocs_memory_cache[2d])
          )
          /
          avg_over_time(nomad_client_allocs_memory_allocated[2d])
          * 100
        )   
    """

    mem_result = prom.custom_query(mem_query)

    cpu = prom.custom_query("""
        100 - avg by (nodename) (
          rate(node_cpu_seconds_total{mode="idle"}[1m]) * 100
        )
    """)

    memory = prom.custom_query("""
        (1 - (
          node_memory_MemAvailable_bytes
          /
          node_memory_MemTotal_bytes
        )) * 100
    """)

    for m in mem_result:
        namespace = m["metric"]["namespace"]
        raw_job_name = m["metric"]["exported_job"]
        base_job = normalize_job_name(raw_job_name)
        idle_job = f"{base_job}-idle"
        

        task_name = m["metric"]["task_group"]
        mem_usage = round(float(m["value"][1]), 2)

        if NAMESPACE not in namespace:
            continue

        # if raw_job_name == idle_job and mem_usage < 10:
        #     print("Already running idle job, skip")
        #     continue

        if mem_usage < 3:
            print(f"[IDLE] {task_name} memory {mem_usage}%")

            if not job_exists(idle_job, namespace):
                print(base_job)
                current_workers = get_job_workers(base_job, namespace)
                print("current_workers:", current_workers)

                target_worker = checkEligibleNode(
                    cpu,
                    memory,
                    exclude_nodes=current_workers
                )
                print("target_worker:", target_worker)

                if not current_workers:
                    print("No eligible worker found, skip idle deploy")
                    continue

                generateJob(
                    base_job=base_job,
                    new_job_name=idle_job,
                    namespace=namespace,
                    target_worker=target_worker,
                    state="iddle"
                )

                if job_has_allocations(idle_job, namespace):
                    stop_job(base_job, namespace)
                else:
                    print("Idle job not started yet, base job NOT stopped")
        else:
            print(f"[BUSY] {task_name} memory {mem_usage}%")
            # raw_job_name = m["metric"]["exported_job"]
            # # base_job = normalize_job_name(raw_job_name)
            # namespace = m["metric"]["namespace"]
            print("Raw_Job_Name:", raw_job_name)
            current_workers = get_job_workers(raw_job_name, namespace)

            print("current_workers:", current_workers)

            target_worker = checkEligibleNode(
                cpu,
                memory,
                exclude_nodes=current_workers
            )

            print("target_worker:", target_worker)
            # Start base job
            generateJob(
                base_job=base_job,
                new_job_name=base_job,
                namespace=namespace,
                target_worker=target_worker,
                state="base"
            )

            # run_base_job(base_job, namespace)

            # Stop idle job if exists
            if job_exists(idle_job, namespace):
                stop_job(idle_job, namespace)
            else:
                print("No idle job to stop")


def main():
    resourceChecker()

if __name__ == "__main__":
    main()