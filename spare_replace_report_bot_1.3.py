import telebot
import pandas as pd
import sqlite3
import schedule
import time
import threading
import logging
import os

from datetime import datetime
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ================= CONFIG =================
DISTRICT_MANAGERS = {

"Dharmapuri":587636725,
"Salem":7887580509,
"Namakkal":838686002,
"Krishinagiri":8641112788,
"Villupuram":8502188931,
"Kallakuruchi":7170102897

}

DISTRICT_REPLACEMENTS = {
    "Vilupuram": "Viluppuram",
    "Villupuram": "Viluppuram"
}


def normalize_district_name(value):
    district = str(value).strip().title()
    if district == "Vilupuram":
        district = "Viluppuram"
    return DISTRICT_REPLACEMENTS.get(district, district)


def normalize_te_name(value):
    te = str(value).replace(".", " ").strip().title()
    te = " ".join(te.split())
    return te


def clean_report_dataframe(df):
    cleaned = df.copy()

    if "district" in cleaned.columns:
        cleaned["district"] = cleaned["district"].astype(str).map(normalize_district_name)

    if "te" in cleaned.columns:
        cleaned["te"] = cleaned["te"].astype(str).map(normalize_te_name)

    if "spare" in cleaned.columns:
        cleaned["spare"] = cleaned["spare"].astype(str).str.strip()

    if "taluk" in cleaned.columns:
        cleaned["taluk"] = cleaned["taluk"].astype(str).str.strip().str.title()

    return cleaned


def validate_clean_data(df):
    if "district" in df.columns:
        district_series = df["district"].astype(str)
        if district_series.str.contains("Vilupuram", case=False, regex=False).any():
            raise ValueError("District normalization failed: found 'Vilupuram'")

    if "te" in df.columns:
        te_series = df["te"].astype(str)
        if te_series.str.contains(r"\.|\s{2,}", regex=True).any():
            raise ValueError("TE normalization failed: inconsistent TE names detected")
        if te_series.str.strip().eq("").any():
            raise ValueError("Validation failed: empty TE names found")


BOT_TOKEN = "8730404668:AAH4DByLGuRbfJ8gVHjPpKINwl5WPGvRqaA"
MANAGER_IDS = [1412356698,587636725,7887580509,838686002,8641112788,8502188931,7170102897]

bot = telebot.TeleBot(BOT_TOKEN)

bot.remove_webhook()
bot.delete_webhook(drop_pending_updates=True)

dropdown_file = "dropdown_details.xlsx"

user = {}
lock = threading.Lock()

# ================= DATABASE =================

conn = sqlite3.connect("shared_inventory.db", check_same_thread=False, timeout=60)

# ✅ ADD THIS (very important)
conn.execute("PRAGMA foreign_keys = ON")

# ✅ Better to execute WAL at connection level
conn.execute("PRAGMA journal_mode=WAL")

cursor = conn.cursor()

# ✅ INDEX FOR FAST SERIAL MATCHING
cursor.execute("CREATE INDEX IF NOT EXISTS idx_devices_serial ON devices(serial_number)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_replacements_serial ON replacements(serial_number)")

conn.commit()


cursor.execute("""
CREATE TABLE IF NOT EXISTS reports(
id INTEGER PRIMARY KEY AUTOINCREMENT,
district TEXT,
taluk TEXT,
fps_code TEXT,
ticket_number TEXT UNIQUE,
complaint_number TEXT,
spare TEXT,
model TEXT,
old_serial TEXT,
new_serial TEXT,
date TEXT,
te TEXT,
shopkeeper_name TEXT,
shopkeeper_mobile TEXT,
remarks TEXT
)
""")

# 🔽 ADD THIS BLOCK HERE
cursor.execute("""
CREATE TABLE IF NOT EXISTS replacements(
serial_number TEXT PRIMARY KEY,
replaced_district TEXT,
replaced_date TEXT
)
""")


# ⭐ ADD INDEX HERE
cursor.execute("CREATE INDEX IF NOT EXISTS idx_ticket ON reports(ticket_number)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_date ON reports(date)")
cursor.execute("CREATE INDEX IF NOT EXISTS idx_district ON reports(district)")

conn.commit()


# ================= SERIAL SEARCH FUNCTION =================

def get_available_serials(search):

    cursor.execute("""
    SELECT serial_number
    FROM devices
    WHERE serial_number LIKE ?
    AND serial_number NOT IN
    (SELECT serial_number FROM replacements)
    """,(f"%{search}%",))

    return [row[0] for row in cursor.fetchall()]

# ================= LOAD DROPDOWN =================

data = pd.read_excel(dropdown_file)

data.columns = data.columns.str.strip()

data["District"] = data["District"].astype(str).map(normalize_district_name)
data["Taluk"] = data["Taluk"].astype(str).str.strip()
data["FPS Code"] = data["FPS Code"].astype(str).str.strip()

districts = sorted(data["District"].dropna().unique())
spares = sorted(data["Name of Spare Replaced"].dropna().unique())

# ================= START COMMAND =================

@bot.message_handler(commands=['start','report'])
def start(message):

    markup = ReplyKeyboardMarkup(resize_keyboard=True)

    for d in districts:
        markup.add(KeyboardButton(d))

    with lock:
        user[message.chat.id] = {"step":"district","data":[]}

    bot.send_message(message.chat.id,"Select District",reply_markup=markup)

# ================= MAIN FLOW =================

@bot.message_handler(func=lambda message: not message.text.startswith("/"))
def handler(message):

    chat = message.chat.id
    text = message.text.strip()

    # Cancel workflow
    if text.lower() == "cancel":
        if chat in user:
            del user[chat]
        bot.send_message(chat,"❌ Process cancelled",reply_markup=ReplyKeyboardRemove())
        return


    if chat not in user:
        return

    step = user[chat]["step"]

    print(f"STEP:{step} | USER:{chat} | TEXT:{text}")

    if step == "district":

        normalized_district = normalize_district_name(text)
        user[chat]["data"].append(normalized_district)

        taluks = data[data["District"] == normalized_district]["Taluk"].unique()

        markup = ReplyKeyboardMarkup(resize_keyboard=True)

        for t in taluks:
            markup.add(KeyboardButton(str(t)))

        user[chat]["step"] = "taluk"

        bot.send_message(chat,"Select Taluk",reply_markup=markup)

    elif step == "taluk":

        user[chat]["data"].append(text)

        fps = data[data["Taluk"]==text]["FPS Code"].unique()

        markup = ReplyKeyboardMarkup(resize_keyboard=True)

        for f in fps:
            markup.add(KeyboardButton(str(f)))

        user[chat]["step"] = "fps"

        bot.send_message(chat,"Select FPS Code",reply_markup=markup)

    elif step == "fps":

        user[chat]["data"].append(text)

        bot.send_message(chat,"Enter Ticket Number",reply_markup=ReplyKeyboardRemove())

        user[chat]["step"] = "ticket"

    elif step == "ticket":

        ticket = text.strip()

        if ticket_exists(ticket):
            bot.send_message(chat,"❌ Ticket already exists. Enter another ticket.")
            return

        user[chat]["data"].append(ticket)

        bot.send_message(chat,"Enter Complaint Number")

        user[chat]["step"] = "complaint"

    elif step == "complaint":

        user[chat]["data"].append(text)    

        markup = ReplyKeyboardMarkup(resize_keyboard=True)

        for s in spares:
            markup.add(KeyboardButton(s))

        user[chat]["step"] = "spare"

        bot.send_message(chat,"Select Spare Replaced",reply_markup=markup)

        return

    elif step == "spare":

        if text.lower() == "others":

            bot.send_message(chat,"Enter Spare Name",reply_markup=ReplyKeyboardRemove())

            user[chat]["step"] = "other_spare"
        else:

             user[chat]["data"].append(text)

             bot.send_message(chat,"Enter Model",reply_markup=ReplyKeyboardRemove())

             user[chat]["step"] = "model"

    elif step == "other_spare":

        spare_name = text.strip()
      
        user[chat]["data"].append(spare_name)

        bot.send_message(chat,"Enter Model")

        user[chat]["step"] = "model"

    elif step == "model":

        user[chat]["data"].append(text)

        bot.send_message(chat,"Enter Old Serial Number (type Nil if not available)")

        user[chat]["step"] = "old_serial"

    elif step == "old_serial":

        user[chat]["data"].append(text)

        spare_type = user[chat]["data"][5]

        # Only Device and IRIS need serial selection
        if spare_type.lower() in ["device","iris"]:

            bot.send_message(chat,"Enter New Serial Number-last 6 digits or full serial number")
            user[chat]["step"] = "new_serial_search"

        else:

            # Leave new_serial blank
            user[chat]["data"].append("")

            bot.send_message(chat,"Enter Name of TE")
            user[chat]["step"] = "te"
    # ================= SERIAL SEARCH =================

    elif step == "new_serial_search":

        serials = get_available_serials(text)

        if not serials:
            bot.send_message(chat,"❌ No matching serial found\nTry again or type Cancel")
            return

        markup = ReplyKeyboardMarkup(resize_keyboard=True)

        for s in serials[:10]:
            markup.add(KeyboardButton(s))

        markup.add(KeyboardButton("Cancel"))

        bot.send_message(chat,"Select Serial Number",reply_markup=markup)

        user[chat]["step"] = "new_serial_select"

    # ================= SERIAL SELECT =================

    elif step == "new_serial_select":
        cursor.execute("""
        SELECT serial_number FROM devices
        WHERE serial_number=? 
        AND serial_number NOT IN (SELECT serial_number FROM replacements)
        """,(text,))

        row = cursor.fetchone() 

        if not row:
            bot.send_message(chat,"❌ Invalid serial selection. Please choose from list.")
            return

        full_serial = row[0]
      
        full_serial = full_serial.strip().upper().replace(" ", "")

        user[chat]["data"].append(full_serial)

        print("Saving serial:", full_serial)

        bot.send_message(chat,"Enter Name of TE",reply_markup=ReplyKeyboardRemove())

        user[chat]["step"] = "te"

    elif step == "te":

        user[chat]["data"].append(normalize_te_name(text))

        bot.send_message(chat,"Enter Shopkeeper Name")

       

        user[chat]["step"] = "shopkeeper_name"

    elif step == "shopkeeper_name":
        
        user[chat]["data"].append(text)

        bot.send_message(chat,"Enter Shopkeeper Mobile Number")

        user[chat]["step"] = "shopkeeper_mobile"

        

    elif step == "shopkeeper_mobile":

        mobile = text.strip()

        if not mobile.isdigit() or len(mobile) != 10:
            bot.send_message(chat,"❌ Invalid mobile number.\nPlease enter a 10 digit mobile number.")
            return

        user[chat]["data"].append(text)
 
        bot.send_message(chat,"Enter Remarks")

        user[chat]["step"] = "remarks"

    elif step == "remarks":

        user[chat]["data"].append(text)

        save_report(user[chat]["data"])

        d = user[chat]["data"]

        summary = f"""
✅ Spare Replacement Report Saved

District : {d[0]}
Taluk : {d[1]}
FPS : {d[2]}
Ticket : {d[3]}
Complaint : {d[4]}
Spare : {d[5]}
Model : {d[6]}
Old Serial : {d[7]}
New Serial : {d[8]}
TE : {d[9]}
Shopkeeper : {d[10]}
Mobile : {d[11]}
Remarks : {d[12]}
"""

        bot.send_message(chat, summary)

        with lock:
            del user[chat]
# ================= DUPLICATE CHECK =================

def ticket_exists(ticket):

    cursor.execute("SELECT ticket_number FROM reports WHERE ticket_number=?", (ticket,))
    return cursor.fetchone() is not None


# ================= REORDER ID FUNCTION =================

def reorder_ids():

    cursor.execute("CREATE TABLE temp_reports AS SELECT * FROM reports ORDER BY id")

    cursor.execute("DROP TABLE reports")

    cursor.execute("""
    CREATE TABLE reports(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    district TEXT,
    taluk TEXT,
    fps_code TEXT,
    ticket_number TEXT UNIQUE,
    complaint_number TEXT,
    spare TEXT,
    model TEXT,
    old_serial TEXT,
    new_serial TEXT,
    date TEXT,
    te TEXT,
    shopkeeper_name TEXT,
    shopkeeper_mobile TEXT,
    remarks TEXT
    )
    """)
    cursor.execute("""
    INSERT INTO reports(
    district,taluk,fps_code,ticket_number,spare,model,
    old_serial,new_serial,date,te,remarks
    )
    SELECT district,taluk,fps_code,ticket_number,spare,model,
    old_serial,new_serial,date,te,remarks
    FROM temp_reports
    """)

    cursor.execute("DROP TABLE temp_reports")

    conn.commit()


# ================= SAVE REPORT =================

def save_report(data):

    try:

        cursor.execute("""
        INSERT INTO reports(
        district,taluk,fps_code,ticket_number,complaint_number,
        spare,model,old_serial,new_serial,date,te,
        shopkeeper_name,shopkeeper_mobile,remarks
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (

            normalize_district_name(data[0]), data[1], data[2], data[3], data[4],
            data[5], data[6], data[7], data[8],
            datetime.today().strftime("%d-%b-%Y"),
            normalize_te_name(data[9]), data[10], data[11], data[12]

        ))

        conn.commit()

        # ================= FIX START =================
        # Use NEW SERIAL (data[8]) instead of old_serial

        if data[8]:

            serial = data[8].strip().upper().replace(" ", "")

            if serial.lower() != "nil":

                cursor.execute("""
                INSERT OR REPLACE INTO replacements(
                serial_number,replaced_district,replaced_date
                )
                VALUES(?,?,?)
                """, (
                    serial,
                    normalize_district_name(data[0]),
                    datetime.today().strftime("%d-%m-%Y")
                ))

                # Update inventory device
                cursor.execute("""
                UPDATE devices
                SET remarks='Replaced'
                WHERE TRIM(UPPER(serial_number))=?
                """, (serial,))

                conn.commit()

        # ================= FIX END =================

    except sqlite3.IntegrityError:
        logging.warning("Duplicate ticket")

# ================= MANAGER COMMAND =================

@bot.message_handler(commands=['today'])
def today_report(message):

    if message.chat.id not in MANAGER_IDS:
        return

    today = datetime.today().strftime("%d-%b-%Y")

    df = pd.read_sql_query("SELECT * FROM reports WHERE date=?",conn,params=[today])
    df = clean_report_dataframe(df)

    if df.empty:
        bot.send_message(message.chat.id,"No reports today")
        return

    summary = df.groupby("district").size()

    msg = "📊 Today's Spare Replacement Report\n\n"

    for d,c in summary.items():
        msg += f"{d} : {c}\n"

    bot.send_message(message.chat.id,msg)

# ================= EXCEL DOWNLOAD =================

@bot.message_handler(commands=['excel'])
def send_excel(message):

    if message.chat.id not in MANAGER_IDS:
        bot.send_message(message.chat.id,"❌ Not authorized")
        return

    file = export_excel()

    try:
        with open(file,"rb") as f:
            bot.send_document(message.chat.id,f)
    except Exception as e:
        bot.send_message(message.chat.id,f"Excel generation failed\n{e}")

# ================= EXPORT EXCEL =================

from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.utils import get_column_letter

# ⭐ Create reports folder automatically
os.makedirs("reports", exist_ok=True)

EMPLOYEE_MASTER_FILE = "Emp_code_name.xlsx"


def load_employee_master():
    emp_df = pd.read_excel(EMPLOYEE_MASTER_FILE)
    emp_df.columns = emp_df.columns.str.strip().str.lower()

    required = {"employee_code", "employee_name"}
    if not required.issubset(set(emp_df.columns)):
        raise ValueError("Emp_code_name.xlsx must contain employee_code and employee_name columns")

    emp_df["employee_code"] = emp_df["employee_code"].astype(str).str.strip().str.upper()
    emp_df["employee_name"] = emp_df["employee_name"].astype(str).replace("nan", "").map(normalize_te_name)
    emp_df["display"] = emp_df["employee_code"] + " - " + emp_df["employee_name"]
    emp_df = emp_df[emp_df["employee_name"].str.strip() != ""].drop_duplicates(subset=["display"])

    if emp_df.empty:
        raise ValueError("Emp_code_name.xlsx does not contain valid employee records")

    return emp_df

def export_excel():

    df = pd.read_sql_query("SELECT * FROM reports", conn)
    df = clean_report_dataframe(df)

    if df.empty:
        raise ValueError("No reports available to export")

    emp_df = load_employee_master()

    validate_clean_data(df)

    if "te" in df.columns and df["te"].astype(str).str.strip().eq("").any():
        raise ValueError("Validation failed: empty TE names found")

    full_report_df = df.copy()
    full_report_df["employee_code"] = (
        full_report_df["te"]
        .astype(str)
        .str.extract(r"^\s*([A-Za-z0-9]+)\s*-\s*.*$", expand=False)
        .fillna("")
    )
    full_report_df["employee_name"] = (
        full_report_df["te"]
        .astype(str)
        .str.extract(r"^\s*[A-Za-z0-9]+\s*-\s*(.*)$", expand=False)
        .fillna("")
    )

    district_summary = (
        df.groupby("district", as_index=False)
        .size()
        .rename(columns={"size": "Total"})
        .sort_values(by="Total", ascending=False)
    )

    te_summary = (
        df.groupby(["te", "district", "taluk"], as_index=False)
        .size()
        .rename(
            columns={
                "te": "TE Name",
                "district": "District",
                "taluk": "Taluk",
                "size": "Total Count"
            }
        )
        .sort_values(by="Total Count", ascending=False)
    )

    spare_summary = (
        df.groupby(["district", "spare"], as_index=False)
        .size()
        .rename(columns={"district": "District", "spare": "Spare Name", "size": "Count"})
        .sort_values(by=["District", "Count"], ascending=[True, False])
    )

    now = datetime.now()

    year = now.strftime("%Y")
    month = now.strftime("%b")

    timestamp = now.strftime("%d-%b-%Y_%H-%M")

    folder = f"reports/{year}/{month}"

    os.makedirs(folder, exist_ok=True)

    file = f"{folder}/Spare_Report_{timestamp}.xlsx"

    # ================= WRITE DATA =================

    with pd.ExcelWriter(file, engine="openpyxl") as writer:

        full_report_df.to_excel(writer, sheet_name="Full Report", index=False)
        district_summary.to_excel(writer, sheet_name="District Summary", index=False)
        te_summary.to_excel(writer, sheet_name="TE Performance", index=False)
        spare_summary.to_excel(writer, sheet_name="Spare-wise District Summary", index=False)
        emp_df[["display", "employee_code", "employee_name"]].to_excel(
            writer,
            sheet_name="Employee Master",
            index=False
        )

    # ================= FORMAT EXCEL =================

    wb = load_workbook(file)

    header_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")

    border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin")
    )

    align = Alignment(horizontal="center", vertical="center")

    for sheet in wb.sheetnames:

        ws = wb[sheet]

        # Freeze header
        ws.freeze_panes = "A2"

        headers = {}

        for cell in ws[1]:
            headers[cell.value] = cell.column_letter

        for row in ws.iter_rows():

            for cell in row:

                cell.border = border
                cell.alignment = align

                if cell.row == 1:
                    cell.font = header_font
                    cell.fill = header_fill

        # ================= DATE FORMAT =================

        if "date" in headers:

            col_letter = headers["date"]

            for cell in ws[col_letter][1:]:
                cell.number_format = "DD-MMM-YYYY"

        # ================= AUTO WIDTH =================

        for col in ws.columns:
            column = col[0].column_letter
            max_length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in col)
            ws.column_dimensions[column].width = max_length + 3

    # ================= TE DROPDOWN + AUTO-FILL =================

    full_ws = wb["Full Report"]
    emp_ws = wb["Employee Master"]

    header_map = {}
    for cell in full_ws[1]:
        header_map[str(cell.value).strip().lower()] = cell.column

    te_col_idx = header_map.get("te")
    emp_code_col_idx = header_map.get("employee_code")
    emp_name_col_idx = header_map.get("employee_name")

    if te_col_idx is None:
        raise ValueError("Validation failed: TE column missing in Full Report sheet")

    max_rows = max(full_ws.max_row, 1000)
    emp_last_row = emp_ws.max_row
    if emp_last_row < 2:
        raise ValueError("Validation failed: employee dropdown list is empty")

    te_col_letter = get_column_letter(te_col_idx)
    emp_code_col_letter = get_column_letter(emp_code_col_idx)
    emp_name_col_letter = get_column_letter(emp_name_col_idx)

    dv = DataValidation(
        type="list",
        formula1=f"'Employee Master'!$A$2:$A${emp_last_row}",
        allow_blank=False
    )
    full_ws.add_data_validation(dv)
    dv.add(f"{te_col_letter}2:{te_col_letter}{max_rows}")

    for row_idx in range(2, max_rows + 1):
        te_cell = f"{te_col_letter}{row_idx}"
        full_ws[f"{emp_code_col_letter}{row_idx}"] = (
            f'=IFERROR(LEFT({te_cell},FIND(" - ",{te_cell})-1),"")'
        )
        full_ws[f"{emp_name_col_letter}{row_idx}"] = (
            f'=IFERROR(MID({te_cell},FIND(" - ",{te_cell})+3,255),"")'
        )

    emp_ws.sheet_state = "hidden"

    # Post-format width update to include formula columns
    for col in full_ws.columns:
        column = col[0].column_letter
        max_length = max(len(str(cell.value)) if cell.value is not None else 0 for cell in col)
        full_ws.column_dimensions[column].width = max_length + 3

    wb.save(file)
    
    return file

# ================= DISTRICT EXCEL EXPORT =================

def export_district_excel(district, df):

    filename = f"{district}_Report.xlsx"

    summary = df.groupby("taluk").size().reset_index(name="Total")

    with pd.ExcelWriter(filename) as writer:
        df.to_excel(writer,sheet_name="Full Report",index=False)
        summary.to_excel(writer,sheet_name="Taluk Summary",index=False)

    return filename



#1️⃣ Add /summary Command
@bot.message_handler(commands=['summary'])
def district_summary(message):

    if message.chat.id not in MANAGER_IDS:
        return

    df = pd.read_sql_query("SELECT * FROM reports", conn)
    df = clean_report_dataframe(df)

    if df.empty:
        bot.send_message(message.chat.id,"No reports available")
        return

    summary = df.groupby("district").size()

    msg = "📊 District Wise Report Summary\n\n"

    for d,c in summary.items():
        msg += f"{d} : {c}\n"

    bot.send_message(message.chat.id,msg)

#Add /district Command

@bot.message_handler(commands=['district'])
def district_report(message):

    if message.chat.id not in MANAGER_IDS:
        return

    parts = message.text.split()

    if len(parts) < 2:
        bot.send_message(message.chat.id,"Usage:\n/district Salem")
        return

    district_name = normalize_district_name(parts[1])

    df = pd.read_sql_query(
        "SELECT * FROM reports WHERE district=?",
        conn,
        params=[district_name]
    )

    if df.empty:
        bot.send_message(message.chat.id,"No reports for this district")
        return

    df = clean_report_dataframe(df)

    msg = f"📊 Reports for {district_name}\n\n"

    for i,row in df.iterrows():
        msg += f"{row['taluk']} | {row['fps_code']} | {row['ticket_number']}\n"

    bot.send_message(message.chat.id,msg)


#1️⃣ /pending command

@bot.message_handler(commands=['pending'])
def pending(message):

    if message.chat.id not in MANAGER_IDS:
        return

    today = datetime.today().strftime("%d-%b-%Y")

    df = pd.read_sql_query(
        "SELECT ticket_number,district,taluk FROM reports WHERE date=?",
        conn,
        params=[today]
    )
    df = clean_report_dataframe(df)

    if df.empty:
        bot.send_message(message.chat.id,"No tickets today")
        return

    msg = "📋 Today's Tickets\n\n"

    for i,row in df.iterrows():
        msg += f"{row['ticket_number']} | {row['district']} | {row['taluk']}\n"

    bot.send_message(message.chat.id,msg)

#2️⃣ /te /te today command (TE performance)

@bot.message_handler(commands=['te'])
def te_summary(message):

    if message.chat.id not in MANAGER_IDS:
        return

    parts = message.text.split()

    # Check if today filter
    if len(parts) > 1 and parts[1].lower() == "today":

        today = datetime.today().strftime("%d-%b-%Y")

        df = pd.read_sql_query(
            "SELECT district,te FROM reports WHERE date=?",
            conn,
            params=[today]
        )
        df = clean_report_dataframe(df)

        title = "📊 Today District-wise TE Performance\n\n"

    else:

        df = pd.read_sql_query(
            "SELECT district,te FROM reports",
            conn
        )
        df = clean_report_dataframe(df)

        title = "📊 Overall District-wise TE Performance\n\n"

    if df.empty:
        bot.send_message(message.chat.id,"No data available")
        return

    msg = title

    districts = df["district"].unique()

    for d in districts:

        ddf = df[df["district"] == d]

        summary = ddf.groupby("te").size()

        total = summary.sum()

        msg += f"📍 {d} (Total : {total})\n"

        for te,count in summary.items():
            msg += f"{te} : {count}\n"

        msg += "\n"

    bot.send_message(message.chat.id,msg)
#3️⃣ /delete ticketnumber

@bot.message_handler(commands=['delete'])
def delete_ticket(message):

    if message.chat.id != 1412356698:
        return

    parts = message.text.split()

    if len(parts) < 2:
        bot.send_message(message.chat.id,"Usage:\n/delete TICKETNUMBER")
        return

    ticket = parts[1]

    # Check if ticket exists
    cursor.execute("SELECT * FROM reports WHERE ticket_number=?", (ticket,))
    row = cursor.fetchone()

    if not row:
        bot.send_message(message.chat.id,"❌ Ticket not found")
        return

    # Delete ticket
    cursor.execute("DELETE FROM reports WHERE ticket_number=?", (ticket,))
    conn.commit()

    reorder_ids()

    bot.send_message(message.chat.id,f"✅ Ticket {ticket} deleted successfully")


#Add /status Command

@bot.message_handler(commands=['status'])
def live_status(message):

    if message.chat.id not in MANAGER_IDS:
        return

    today = datetime.today().strftime("%d-%b-%Y")

    df = pd.read_sql_query(
        "SELECT * FROM reports WHERE date=?",
        conn,
        params=[today]
    )
    df = clean_report_dataframe(df)

    if df.empty:
        bot.send_message(message.chat.id,"No reports today")
        return

    summary = df.groupby("district").size()

    msg = "📊 Live Spare Dashboard\n\n"

    total = 0

    for d,c in summary.items():
        msg += f"{d} : {c}\n"
        total += c

    msg += f"\nTotal Today : {total}"

    bot.send_message(message.chat.id,msg)


# ================= STOCK COMMAND =================

@bot.message_handler(commands=['stock'])
def stock_status(message):

    if message.chat.id not in MANAGER_IDS:
        return

    try:

        device_count = cursor.execute("""
        SELECT COUNT(*) FROM devices
        WHERE model LIKE '%A%' 
        AND (remarks IS NULL OR remarks!='Replaced')
        """).fetchone()[0]

        iris_count = cursor.execute("""
        SELECT COUNT(*) FROM devices
        WHERE model LIKE '%IRIS%' 
        AND (remarks IS NULL OR remarks!='Replaced')
        """).fetchone()[0]

        printer_count = cursor.execute("""
        SELECT COUNT(*) FROM devices
        WHERE model LIKE '%PRINTER%' 
        AND (remarks IS NULL OR remarks!='Replaced')
        """).fetchone()[0]

        msg = f"""
       📦 Live Inventory Stock

       Device : {device_count}
       IRIS : {iris_count}
       Printer : {printer_count}
       """

        bot.send_message(message.chat.id,msg)

    except Exception as e:
        bot.send_message(message.chat.id,f"Error reading stock\n{e}")

# ================= FIND TICKET COMMAND =================

@bot.message_handler(commands=['find'])
def find_ticket(message):

    if message.chat.id not in MANAGER_IDS:
        return

    parts = message.text.split()

    if len(parts) < 2:
        bot.send_message(message.chat.id,"Usage:\n/find TICKETNUMBER")
        return

    ticket = parts[1]

    cursor.execute("SELECT * FROM reports WHERE ticket_number=?", (ticket,))
    row = cursor.fetchone()

    if not row:
        bot.send_message(message.chat.id,"❌ Ticket not found")
        return

    msg = f"""
     🔎 Ticket Details

     District : {row[1]}
     Taluk : {row[2]}
     FPS : {row[3]}
     Ticket : {row[4]}
     Spare : {row[5]}
     Model : {row[6]}
     Old Serial : {row[7]}
     New Serial : {row[8]}
     Date : {row[9]}
     TE : {row[10]}
     Remarks : {row[11]}
     """

    bot.send_message(message.chat.id,msg)

#fallback command -you know the bot is alive

@bot.message_handler(commands=['ping'])
def ping(message):
    bot.send_message(message.chat.id,"✅ Bot running")

#Add /district_excel Command

@bot.message_handler(commands=['district_excel'])
def district_excel(message):

    if message.chat.id not in MANAGER_IDS:
        bot.send_message(message.chat.id,"❌ Not authorized")
        return

    parts = message.text.split()

    if len(parts) < 2:
        bot.send_message(message.chat.id,"Usage:\n/district_excel Salem")
        return

    district_name = parts[1]

    df = pd.read_sql_query(
        "SELECT * FROM reports WHERE district=?",
        conn,
        params=[district_name]
    )

    if df.empty:
        bot.send_message(message.chat.id,"No reports for this district")
        return

    file = export_district_excel(district_name, df)

    with open(file,"rb") as f:
        bot.send_document(message.chat.id,f)

    os.remove(file)

#🔟 Add /cancel Command

@bot.message_handler(commands=['cancel'])
def cancel_process(message):

    chat = message.chat.id

    if chat in user:
        del user[chat]

        bot.send_message(chat,"❌ Process cancelled",reply_markup=ReplyKeyboardRemove())

    else:
        bot.send_message(chat,"No active process running.")


#1️⃣ Create Backup Function

import shutil

def backup_database():

    today = datetime.today().strftime("%Y-%m-%d")

    os.makedirs("db_backups", exist_ok=True)

    backup_file = f"db_backups/inventory_backup_{today}.db"

    conn.commit()
    shutil.copy("shared_inventory.db", backup_file)

    print("Database backup created:", backup_file)

#2️⃣ Delete Records Older Than 30 Days

def delete_old_reports():

    cursor.execute("""
    DELETE FROM reports
    WHERE date < date('now','-30 day')
    """)

    conn.commit()

    print("Old records deleted (older than 30 days)")



# ================= DAILY AUTO REPORT =================

def send_daily_report():

    today = datetime.today().strftime("%d-%b-%Y")

    df = pd.read_sql_query(
        "SELECT * FROM reports WHERE date=?",
        conn,
        params=[today]
    )

    if df.empty:
        return

    # Send report to each district manager
    for district, manager in DISTRICT_MANAGERS.items():

        ddf = df[df["district"] == district]

        if ddf.empty:
            continue

        msg = f"📊 Daily Spare Replacement Report\n\nDistrict : {district}\nTotal : {len(ddf)}"

        bot.send_message(manager,msg)

        filename = export_district_excel(district, ddf)

        with open(filename,"rb") as file:
            bot.send_document(manager,file)

        os.remove(filename)
    
    file = export_excel()
    with open(file,"rb") as f:
        bot.send_document(1412356698,f)
    os.remove(file)

#Automatically delete old backup files after 30 days.

def clean_old_backups():

    folder = "db_backups"

    if not os.path.exists(folder):
        return

    now = time.time()

    for file in os.listdir(folder):

        path = os.path.join(folder,file)

        if os.stat(path).st_mtime < now - 30*86400:
            os.remove(path)

#To make the bot list all commands when a user types /commands or /help,

@bot.message_handler(commands=['commands','help'])
def command_list(message):

    msg = """
📌 Spare Replacement Bot – Command List

🔧 Reporting
/start  - Start spare replacement report
/report - Start spare replacement report

📊 Reports
/today    - Today's district report
/summary  - District wise summary
/status   - Live spare dashboard
/pending  - Today's ticket list

📄 Excel Reports
/excel                     - Download full Excel report
/district_excel DISTRICT   - District Excel report

Example:
/district_excel Salem

🔎 Search
/find TICKETNUMBER

Example:
/find TKT12345

👨‍🔧 TE Performance
/te - Technician report count

📦 Inventory
/stock - Show available device / iris / printer stock

🗂 District Reports
/district DISTRICTNAME

Example:
/district Salem

🗑 Admin
/delete TICKETNUMBER  (Admin only)

⚙ System
/ping - Check bot status

❌ Cancel Process
Type **Cancel** anytime to stop reporting
"""

    bot.send_message(message.chat.id, msg)


# ================= SCHEDULER =================

def scheduler():

    schedule.every().day.at("18:00").do(send_daily_report)

    schedule.every().day.at("23:50").do(backup_database)

    schedule.every().day.at("23:55").do(delete_old_reports)

    schedule.every().day.at("23:58").do(clean_old_backups)

    while True:
        schedule.run_pending()
        time.sleep(10)

# ================= BOT RUNNER =================

def run_bot():

    while True:

        try:

            print("Bot running...")

            bot.remove_webhook()
            time.sleep(2)

            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True
            )

        except Exception as e:

            logging.exception("Bot crashed")

            print("Restarting bot in 5 seconds...")

            time.sleep(5)

run_bot()
