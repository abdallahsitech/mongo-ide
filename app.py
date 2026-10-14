from flask import Flask, render_template, request, jsonify, Response
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure
from bson import ObjectId
from bson.json_util import dumps, loads
import json, csv, io, datetime, os, re, traceback, openpyxl
import anthropic
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

CONNECTIONS_FILE = os.path.join(os.path.dirname(__file__), "connections.json")
connections_config = {}
active_connections = {}

# ─── Persistence ──────────────────────────────────────────────────────────────

def load_connections():
    global connections_config
    if os.path.exists(CONNECTIONS_FILE):
        with open(CONNECTIONS_FILE) as f:
            connections_config = json.load(f)

def save_connections():
    with open(CONNECTIONS_FILE, "w") as f:
        json.dump(connections_config, f, indent=2)

load_connections()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def serial(doc):
    return json.loads(dumps(doc))

def get_client(name):
    if name not in active_connections:
        cfg = connections_config.get(name)
        if not cfg:
            raise ValueError(f"Connection '{name}' not found")
        client = MongoClient(cfg["uri"], serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        active_connections[name] = client
    return active_connections[name]

def split_args(s):
    """Split top-level comma-separated arguments."""
    parts, cur, depth, in_str, sc = [], [], 0, False, None
    for ch in s:
        if in_str:
            if ch == sc:
                in_str = False
            cur.append(ch)
        elif ch in "\"'":
            in_str, sc = True, ch
            cur.append(ch)
        elif ch in "{[(":
            depth += 1; cur.append(ch)
        elif ch in "}])":
            depth -= 1; cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip()); cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts

def execute_shell_cmd(client, db_name, cmd):
    cmd = cmd.strip()
    if cmd in ("show dbs", "show databases"):
        return "\n".join(client.list_database_names())
    if cmd in ("show collections", "show tables"):
        return "\n".join(client[db_name].list_collection_names())
    if cmd.startswith("use "):
        return f"switched to db {cmd[4:].strip()}"
    if cmd == "db.stats()":
        return dumps(client[db_name].command("dbstats"), indent=2)
    if cmd == "db.getName()":
        return f'"{db_name}"'

    # db.collection.method(args)
    m = re.match(r"db\.(\w+)\.(\w+)\((.*)\)$", cmd, re.DOTALL)
    if m:
        col_name, method, args_str = m.group(1), m.group(2), m.group(3).strip()
        col = client[db_name][col_name]
        try:
            if method == "find":
                parts = split_args(args_str)
                f = loads(parts[0]) if parts and parts[0] else {}
                p = loads(parts[1]) if len(parts) > 1 else None
                return dumps(list(col.find(f, p).limit(50)), indent=2)
            if method == "findOne":
                f = loads(args_str) if args_str else {}
                return dumps(col.find_one(f), indent=2)
            if method == "insertOne":
                r = col.insert_one(loads(args_str))
                return f'{{"acknowledged":true,"insertedId":"{r.inserted_id}"}}'
            if method == "insertMany":
                docs = loads(args_str)
                r = col.insert_many(docs)
                return f'{{"acknowledged":true,"insertedCount":{len(r.inserted_ids)}}}'
            if method == "updateOne":
                parts = split_args(args_str)
                r = col.update_one(loads(parts[0]), loads(parts[1]))
                return f'{{"acknowledged":true,"matchedCount":{r.matched_count},"modifiedCount":{r.modified_count}}}'
            if method == "updateMany":
                parts = split_args(args_str)
                r = col.update_many(loads(parts[0]), loads(parts[1]))
                return f'{{"acknowledged":true,"matchedCount":{r.matched_count},"modifiedCount":{r.modified_count}}}'
            if method == "deleteOne":
                r = col.delete_one(loads(args_str))
                return f'{{"acknowledged":true,"deletedCount":{r.deleted_count}}}'
            if method == "deleteMany":
                r = col.delete_many(loads(args_str))
                return f'{{"acknowledged":true,"deletedCount":{r.deleted_count}}}'
            if method == "countDocuments":
                f = loads(args_str) if args_str else {}
                return str(col.count_documents(f))
            if method == "distinct":
                field = args_str.strip("\"'")
                return dumps(col.distinct(field), indent=2)
            if method == "drop":
                col.drop(); return "true"
            if method == "createIndex":
                parts = split_args(args_str)
                keys = [(k, v) for k, v in loads(parts[0]).items()]
                opts = loads(parts[1]) if len(parts) > 1 else {}
                return f'"{col.create_index(keys, **opts)}"'
            if method == "getIndexes":
                return dumps(list(col.list_indexes()), indent=2)
            if method == "aggregate":
                pipeline = loads(args_str)
                return dumps(list(col.aggregate(pipeline)), indent=2)
            if method == "stats":
                return dumps(client[db_name].command("collstats", col_name), indent=2)
            if method == "explain":
                parts = split_args(args_str)
                f = loads(parts[0]) if parts else {}
                return dumps(col.find(f).explain(), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # db.runCommand(...)
    m2 = re.match(r"db\.runCommand\((.*)\)$", cmd, re.DOTALL)
    if m2:
        try:
            return dumps(client[db_name].command(loads(m2.group(1))), indent=2)
        except Exception as e:
            return f"Error: {e}"

    return f"Error: Unrecognized command: {cmd}"

def parse_where(s):
    s = s.strip()
    if re.search(r'\bAND\b', s, re.IGNORECASE):
        parts = re.split(r'\bAND\b', s, flags=re.IGNORECASE)
        r = {}
        for p in parts:
            r.update(parse_where(p))
        return r
    if re.search(r'\bOR\b', s, re.IGNORECASE):
        parts = re.split(r'\bOR\b', s, flags=re.IGNORECASE)
        return {"$or": [parse_where(p) for p in parts]}
    for op, mop in [(">=", "$gte"), ("<=", "$lte"), ("!=", "$ne"), (">", "$gt"), ("<", "$lt"), ("=", "")]:
        if op in s:
            field, val_s = s.split(op, 1)
            field, val_s = field.strip(), val_s.strip().strip("\"'")
            try:
                val = json.loads(val_s)
            except:
                val = val_s
            return {field: ({mop: val} if mop else val)}
    return {}

def sql_to_mongo(sql):
    sql = sql.strip().rstrip(";")
    m = re.match(
        r"SELECT\s+(.*?)\s+FROM\s+(\w+)(?:\s+WHERE\s+(.*?))?(?:\s+ORDER\s+BY\s+(.*?))?(?:\s+LIMIT\s+(\d+))?$",
        sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return {"error": "Cannot parse SQL. Supported: SELECT ... FROM ... [WHERE ...] [ORDER BY ...] [LIMIT n]"}
    fields_s, collection = m.group(1).strip(), m.group(2).strip()
    where_s, order_s, limit_s = m.group(3), m.group(4), m.group(5)
    proj = {} if fields_s == "*" else {f.strip(): 1 for f in fields_s.split(",")}
    filt = parse_where(where_s) if where_s else {}
    sort = {}
    if order_s:
        for part in order_s.split(","):
            part = part.strip()
            if re.search(r'\bDESC\b', part, re.IGNORECASE):
                sort[re.sub(r'\s+DESC\s*$', '', part, flags=re.IGNORECASE).strip()] = -1
            else:
                sort[re.sub(r'\s+ASC\s*$', '', part, flags=re.IGNORECASE).strip()] = 1
    limit = int(limit_s) if limit_s else None
    proj_s = json.dumps(proj) if proj else "{}"
    sort_s = f".sort({json.dumps(sort)})" if sort else ""
    lim_s = f".limit({limit})" if limit else ""
    shell = f"db.{collection}.find({json.dumps(filt, indent=2)}, {proj_s}){sort_s}{lim_s}"
    return {"collection": collection, "filter": filt, "projection": proj,
            "sort": sort, "limit": limit, "shell_command": shell}

def parse_doc_id(doc_id):
    """Parse a doc_id string into an ObjectId, int, or string."""
    try:
        return ObjectId(doc_id)
    except Exception:
        try:
            return int(doc_id)
        except (ValueError, TypeError):
            return doc_id

def flatten_doc(doc, prefix=""):
    r = {}
    for k, v in doc.items():
        fk = f"{prefix}{k}"
        if isinstance(v, dict):
            r.update(flatten_doc(v, f"{fk}."))
        elif isinstance(v, list):
            r[fk] = json.dumps(v)
        else:
            r[fk] = v
    return r

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

# Connections
@app.route("/api/connections", methods=["GET"])
def list_conns():
    result = []
    for name, cfg in connections_config.items():
        c = dict(cfg)
        c["connected"] = name in active_connections
        result.append(c)
    return jsonify(result)

@app.route("/api/connections", methods=["POST"])
def add_conn():
    d = request.json
    name = d.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name required"}), 400
    connections_config[name] = {"name": name, "uri": d.get("uri", "mongodb://localhost:27017"), "color": d.get("color", "#00ED64")}
    save_connections()
    return jsonify({"success": True, "name": name})

@app.route("/api/connections/<name>", methods=["DELETE"])
def del_conn(name):
    if name in active_connections:
        active_connections[name].close(); del active_connections[name]
    connections_config.pop(name, None)
    save_connections()
    return jsonify({"success": True})

@app.route("/api/connections/<name>/connect", methods=["POST"])
def connect_db(name):
    try:
        get_client(name)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/disconnect", methods=["POST"])
def disconnect_db(name):
    if name in active_connections:
        active_connections[name].close(); del active_connections[name]
    return jsonify({"success": True})

# Databases
@app.route("/api/connections/<name>/databases")
def list_dbs(name):
    try:
        client = get_client(name)
        result = []
        for db_name in client.list_database_names():
            cols = client[db_name].list_collection_names()
            result.append({"name": db_name, "collections_count": len(cols)})
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases", methods=["POST"])
def create_db(name):
    try:
        d = request.json
        get_client(name)[d["name"]].create_collection(d.get("collection", "default"))
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases/<db>", methods=["DELETE"])
def drop_db(name, db):
    try:
        get_client(name).drop_database(db)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Collections
@app.route("/api/connections/<name>/databases/<db>/collections")
def list_cols(name, db):
    try:
        client = get_client(name)
        result = []
        for col in client[db].list_collections():
            col_name = col["name"]
            col_type = col.get("type", "collection")
            try:
                if col_type == "view":
                    count, size = 0, 0
                else:
                    count = client[db][col_name].estimated_document_count()
                    size  = 0
            except Exception:
                count, size = 0, 0
            result.append({"name": col_name, "count": count, "size": size, "type": col_type})
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases/<db>/collections", methods=["POST"])
def create_col(name, db):
    try:
        d = request.json
        get_client(name)[db].create_collection(d["name"])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases/<db>/collections/<col>", methods=["DELETE"])
def drop_col(name, db, col):
    try:
        get_client(name)[db][col].drop()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Documents
@app.route("/api/connections/<name>/databases/<db>/collections/<col>/documents")
def get_docs(name, db, col):
    try:
        client = get_client(name)
        c = client[db][col]
        try:
            filt = loads(request.args.get("filter", "{}"))
        except:
            filt = {}
        try:
            sort_d = loads(request.args.get("sort", "{}"))
        except:
            sort_d = {}
        try:
            proj = loads(request.args.get("projection", "{}")) or None
        except:
            proj = None
        skip = int(request.args.get("skip", 0))
        limit = int(request.args.get("limit", 20))
        total = c.count_documents(filt)
        cur = c.find(filt, proj)
        if sort_d:
            cur = cur.sort([(k, ASCENDING if v == 1 else DESCENDING) for k, v in sort_d.items()])
        docs = [serial(d) for d in cur.skip(skip).limit(limit)]
        return jsonify({"documents": docs, "total": total, "skip": skip, "limit": limit})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases/<db>/collections/<col>/documents", methods=["POST"])
def insert_doc(name, db, col):
    try:
        d = request.json
        c = get_client(name)[db][col]
        doc = loads(json.dumps(d.get("document", {})))
        r = c.insert_one(doc)
        return jsonify({"success": True, "inserted_id": str(r.inserted_id)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases/<db>/collections/<col>/documents/<doc_id>", methods=["PUT"])
def update_doc(name, db, col, doc_id):
    try:
        d = request.json
        c = get_client(name)[db][col]
        doc = loads(json.dumps(d.get("document", {})))
        doc.pop("_id", None)
        r = c.replace_one({"_id": parse_doc_id(doc_id)}, doc)
        return jsonify({"success": True, "modified": r.modified_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases/<db>/collections/<col>/documents/<doc_id>", methods=["DELETE"])
def del_doc(name, db, col, doc_id):
    try:
        c = get_client(name)[db][col]
        r = c.delete_one({"_id": parse_doc_id(doc_id)})
        return jsonify({"success": True, "deleted": r.deleted_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Aggregation
@app.route("/api/connections/<name>/databases/<db>/collections/<col>/aggregate", methods=["POST"])
def aggregate(name, db, col):
    try:
        d = request.json
        pipeline = d.get("pipeline", [])
        if isinstance(pipeline, str):
            pipeline = loads(pipeline)
        c = get_client(name)[db][col]
        results = list(c.aggregate(pipeline))
        return jsonify({"results": [serial(r) for r in results], "count": len(results)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Shell
@app.route("/api/connections/<name>/shell", methods=["POST"])
def shell(name):
    try:
        d = request.json
        db_name = d.get("database", "admin")
        result = execute_shell_cmd(get_client(name), db_name, d.get("command", ""))
        return jsonify({"result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Schema
@app.route("/api/connections/<name>/databases/<db>/collections/<col>/schema")
def schema(name, db, col):
    try:
        client = get_client(name)
        c = client[db][col]
        sample_size = int(request.args.get("sample", 100))
        total = c.count_documents({})
        docs = list(c.find({}).limit(sample_size))

        def val_type(v):
            if v is None: return "null"
            if isinstance(v, bool): return "boolean"
            if isinstance(v, int): return "integer"
            if isinstance(v, float): return "double"
            if isinstance(v, str): return "string"
            if isinstance(v, list): return "array"
            if isinstance(v, dict): return "object"
            if isinstance(v, datetime.datetime): return "date"
            if isinstance(v, ObjectId): return "objectId"
            return type(v).__name__

        fields = {}
        def analyze(doc, prefix=""):
            for k, v in doc.items():
                fk = f"{prefix}{k}"
                if fk not in fields:
                    fields[fk] = {"types": {}, "count": 0}
                t = val_type(v)
                fields[fk]["types"][t] = fields[fk]["types"].get(t, 0) + 1
                fields[fk]["count"] += 1
                if isinstance(v, dict):
                    analyze(v, f"{fk}.")

        for doc in docs:
            analyze(doc)

        result = []
        for field, info in fields.items():
            dominant = max(info["types"], key=info["types"].get) if info["types"] else "unknown"
            result.append({"field": field, "type": dominant, "types": info["types"],
                           "coverage": round(info["count"] / len(docs) * 100, 1) if docs else 0})
        return jsonify({"schema": result, "sampled": len(docs), "total": total})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Fields (for autocomplete)
@app.route("/api/connections/<name>/databases/<db>/collections/<col>/fields")
def get_fields(name, db, col):
    try:
        client = get_client(name)
        c = client[db][col]
        docs = list(c.find({}).limit(20))
        fields = set()
        def collect(doc, prefix=""):
            for k, v in doc.items():
                fk = f"{prefix}{k}"
                fields.add(fk)
                if isinstance(v, dict):
                    collect(v, f"{fk}.")
        for doc in docs:
            collect(doc)
        return jsonify({"fields": sorted(fields)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Indexes
@app.route("/api/connections/<name>/databases/<db>/collections/<col>/indexes")
def get_indexes(name, db, col):
    try:
        indexes = list(get_client(name)[db][col].list_indexes())
        return jsonify([serial(i) for i in indexes])
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases/<db>/collections/<col>/indexes", methods=["POST"])
def create_index(name, db, col):
    try:
        d = request.json
        c = get_client(name)[db][col]
        keys = [(k, v) for k, v in d.get("keys", {}).items()]
        opts = {k: v for k, v in d.get("options", {}).items() if v}
        idx_name = c.create_index(keys, **opts)
        return jsonify({"success": True, "index_name": idx_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/connections/<name>/databases/<db>/collections/<col>/indexes/<idx>", methods=["DELETE"])
def drop_index(name, db, col, idx):
    try:
        get_client(name)[db][col].drop_index(idx)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Export
@app.route("/api/connections/<name>/databases/<db>/collections/<col>/export")
def export_data(name, db, col):
    try:
        client = get_client(name)
        c = client[db][col]
        fmt = request.args.get("format", "json")
        try:
            filt = loads(request.args.get("filter", "{}"))
        except:
            filt = {}
        docs = list(c.find(filt))
        if fmt == "json":
            return Response(dumps(docs, indent=2), mimetype="application/json",
                            headers={"Content-Disposition": f"attachment;filename={col}.json"})
        elif fmt == "csv":
            flat = [flatten_doc(serial(d)) for d in docs]
            keys = sorted(set(k for d in flat for k in d))
            out = io.StringIO()
            w = csv.DictWriter(out, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            for d in flat:
                w.writerow({k: d.get(k, "") for k in keys})
            return Response(out.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment;filename={col}.csv"})
        elif fmt == "xlsx":
            flat = [flatten_doc(serial(d)) for d in docs]
            keys = sorted(set(k for d in flat for k in d))
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = col[:31]  # Excel sheet name max 31 chars
            # Header row with styling
            for ci, key in enumerate(keys, 1):
                cell = ws.cell(row=1, column=ci, value=key)
                cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF")
                cell.fill = openpyxl.styles.PatternFill(fill_type="solid", fgColor="00854C")
            # Data rows
            for ri, doc in enumerate(flat, 2):
                for ci, key in enumerate(keys, 1):
                    val = doc.get(key, "")
                    ws.cell(row=ri, column=ci, value=str(val) if not isinstance(val, (int, float, bool, type(None))) else val)
            # Auto-width columns
            for col_cells in ws.columns:
                max_len = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
                ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 50)
            out = io.BytesIO()
            wb.save(out)
            out.seek(0)
            return Response(out.read(),
                            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f"attachment;filename={col}.xlsx"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Import
@app.route("/api/connections/<name>/databases/<db>/collections/<col>/import", methods=["POST"])
def import_data(name, db, col):
    try:
        c = get_client(name)[db][col]
        fmt = request.form.get("format", "json")
        if "file" not in request.files:
            return jsonify({"error": "No file"}), 400
        content = request.files["file"].read()
        if fmt == "json":
            docs = loads(content)
            if isinstance(docs, dict):
                docs = [docs]
            r = c.insert_many(docs)
            return jsonify({"success": True, "inserted": len(r.inserted_ids)})
        elif fmt == "csv":
            reader = csv.DictReader(io.StringIO(content.decode("utf-8")))
            docs = list(reader)
            if docs:
                r = c.insert_many(docs)
                return jsonify({"success": True, "inserted": len(r.inserted_ids)})
            return jsonify({"success": True, "inserted": 0})
        elif fmt == "xlsx":
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.rows)
            if not rows:
                return jsonify({"success": True, "inserted": 0})
            headers = [cell.value for cell in rows[0]]
            docs = []
            for row in rows[1:]:
                doc = {}
                for header, cell in zip(headers, row):
                    if header is not None:
                        val = cell.value
                        doc[str(header)] = val if val is not None else ""
                docs.append(doc)
            if docs:
                r = c.insert_many(docs)
                return jsonify({"success": True, "inserted": len(r.inserted_ids)})
            return jsonify({"success": True, "inserted": 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# SQL
@app.route("/api/sql", methods=["POST"])
def sql_endpoint():
    try:
        d = request.json
        result = sql_to_mongo(d.get("sql", ""))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Data Compare
@app.route("/api/compare", methods=["POST"])
def compare():
    try:
        d = request.json
        src = get_client(d["source_connection"])[d["source_db"]][d["source_collection"]]
        tgt = get_client(d["target_connection"])[d["target_db"]][d["target_collection"]]
        src_docs = {str(x["_id"]): x for x in src.find({})}
        tgt_docs = {str(x["_id"]): x for x in tgt.find({})}
        only_src = [i for i in src_docs if i not in tgt_docs]
        only_tgt = [i for i in tgt_docs if i not in src_docs]
        in_both = [i for i in src_docs if i in tgt_docs]
        diff = [i for i in in_both if dumps(src_docs[i]) != dumps(tgt_docs[i])]
        return jsonify({"only_in_source": len(only_src), "only_in_target": len(only_tgt),
                        "matching": len(in_both) - len(diff), "different": len(diff),
                        "sample_diff_ids": diff[:5], "sample_src_ids": only_src[:5], "sample_tgt_ids": only_tgt[:5]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# AI Query Helper
def _build_ai_system(context):
    parts = []
    if context.get("conn"):        parts.append(f"Connection : {context['conn']}")
    if context.get("db"):          parts.append(f"Database   : {context['db']}")
    if context.get("collection"):  parts.append(f"Collection : {context['collection']}")
    if context.get("fields"):
        parts.append(f"Known fields: {', '.join(context['fields'][:60])}")
    ctx = "\n".join(parts) if parts else "No collection selected yet."
    return f"""You are an expert MongoDB assistant embedded inside a MongoDB GUI tool.

Current working context:
{ctx}

Your role:
- Help the user write MongoDB queries, aggregation pipelines, shell commands, and indexes.
- Always use the actual collection name from the context when writing code.
- Format ALL query code inside triple-backtick code blocks (```).
- After each query, briefly explain what it does and any important notes (indexes, edge cases).
- If no collection is selected, still provide a generic example but mention they should select one.
- When suggesting aggregations, write the full pipeline array.
- Be concise. Lead with the ready-to-use query, then explain.
"""

@app.route("/api/ai/query", methods=["POST"])
def ai_query():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY environment variable is not set."}), 400
    try:
        d = request.json
        messages = d.get("messages", [])
        context = d.get("context", {})
        system_prompt = _build_ai_system(context)
        client_ai = anthropic.Anthropic(api_key=api_key)

        def generate():
            try:
                with client_ai.messages.stream(
                    model="claude-opus-4-6",
                    max_tokens=2048,
                    system=system_prompt,
                    messages=messages
                ) as stream:
                    for text in stream.text_stream:
                        yield f"data: {json.dumps({'text': text})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

if __name__ == "__main__":
    app.run(debug=True, port=8080, host="0.0.0.0")
