from flask import Flask, jsonify
from flask_cors import CORS
import serial
import time
import requests
import sqlite3
import logging
from datetime import datetime
import sys
import joblib
import numpy as np

app = Flask(__name__)
CORS(app)
# LOAD TRAINED ML MODEL
model = joblib.load('solar_model.pkl')
# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('solar_dashboard.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# YOUR COM PORT
PORT = 'COM3'
API_KEY = '6c87f822ebdd5f83c409031881eee79b'
CITY = 'Jaipur'
ARDUINO_TIMEOUT = 3

# Connect Arduino with error handling
arduino = None
try:
    arduino = serial.Serial(PORT, 9600, timeout=ARDUINO_TIMEOUT)
    time.sleep(2)
    logger.info(f"[CONNECTED] Arduino connected on {PORT}")
except serial.SerialException as e:
    logger.error(f"[ARDUINO_FAIL] Failed to connect Arduino on {PORT}: {e}")
    logger.warning("[FALLBACK] App will run in demo mode without Arduino")
except Exception as e:
    logger.error(f"[ERROR] Unexpected error connecting Arduino: {e}")
    logger.warning("[FALLBACK] App will run in demo mode without Arduino")
# DATABASE SETUP
conn = sqlite3.connect('database.db', check_same_thread=False)

cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS sensor_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ldr_left TEXT,
    ldr_right TEXT,
    servo_angle TEXT,
    voltage TEXT,
    temperature TEXT,
    humidity TEXT,
    weather TEXT,
    timestamp TEXT
)
''')

conn.commit()

# Global Error Handler
@app.errorhandler(Exception)
def handle_error(error):
    logger.error(f"[UNHANDLED] Unhandled error: {error}")
    return jsonify({"error": "Internal server error", "details": str(error)}), 500

@app.route('/')
def home():
    return "Backend Running Successfully"

@app.route('/sensor-data')
def sensor_data():
    try:
        # READ ARDUINO DATA
        if arduino and arduino.is_open:
            try:
                # Read data with multiple attempts for complete data
                raw_data = ""
                attempts = 0
                while attempts < 5 and len(raw_data.split(',')) < 4:
                    chunk = arduino.readline().decode('utf-8', errors='ignore').strip()
                    if chunk:
                        raw_data += chunk
                    attempts += 1
                    time.sleep(0.05)
                
                if not raw_data:
                    logger.warning("[WARNING] Arduino returned empty data - using demo")
                    values = ["500", "520", "45", "12.5"]
                else:
                    # Clean and parse data
                    raw_data = raw_data.replace('\n', '').replace('\r', '').replace(' ', '')
                    values = [v.strip() for v in raw_data.split(',') if v.strip()]
                    
                    if len(values) < 4:
                        logger.warning(f"[WARNING] Incomplete data ({len(values)} values): {raw_data} - using demo")
                        values = ["500", "520", "45", "12.5"]
                    else:
                        values = values[:4]
                        logger.debug(f"[OK] Arduino: LDR_L={values[0]}, LDR_R={values[1]}, Angle={values[2]}, Volt={values[3]}")
                        
            except serial.SerialException as e:
                logger.error(f"[ARDUINO_ERROR] {e} - using demo")
                values = ["500", "520", "45", "12.5"]
            except Exception as e:
                logger.error(f"[PARSE_ERROR] {e} - using demo")
                values = ["500", "520", "45", "12.5"]
        else:
            logger.debug("[INFO] Arduino not connected - using demo data")
            values = ["500", "520", "45", "12.5"]

        # WEATHER API with retry
        temperature = humidity = weather = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                weather_url = f"https://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={API_KEY}&units=metric"
                weather_response = requests.get(weather_url, timeout=5).json()
                
                if 'error' in weather_response:
                    logger.warning(f"[WEATHER_ERROR] {weather_response['error']}")
                else:
                    temperature = weather_response['main']['temp']
                    humidity = weather_response['main']['humidity']
                    weather = weather_response['weather'][0]['main']
                    logger.debug(f"[WEATHER_OK] Temp={temperature}C, Humidity={humidity}%, Weather={weather}")
                    break
            except requests.exceptions.Timeout:
                logger.warning(f"[TIMEOUT] Weather API (attempt {attempt + 1}/{max_retries})")
            except requests.exceptions.RequestException as e:
                logger.warning(f"[API_ERROR] Weather (attempt {attempt + 1}/{max_retries}): {str(e)[:50]}")
            except Exception as e:
                logger.warning(f"[ERROR] Weather fetch: {str(e)[:50]}")
            
            if attempt < max_retries - 1:
                time.sleep(1)
        
        # Use defaults if API failed
        if temperature is None:
            temperature = 25.0
            humidity = 50
            weather = "Unknown"
            logger.info("[FALLBACK] Using default weather values")

        # SAVE TO DATABASE
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            cursor.execute('''
INSERT INTO sensor_data (
    ldr_left,
    ldr_right,
    servo_angle,
    voltage,
    temperature,
    humidity,
    weather,
    timestamp
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
''', (
                values[0],
                values[1],
                values[2],
                values[3],
                str(temperature),
                str(humidity),
                str(weather),
                timestamp
            ))
            conn.commit()
            logger.debug("[DB_OK] Data saved to database")
        except sqlite3.Error as db_error:
            logger.error(f"[DB_ERROR] {db_error}")
            conn.rollback()

        response = {
            "ldr_left": values[0],
            "ldr_right": values[1],
            "servo_angle": values[2],
            "voltage": values[3],
            "temperature": temperature,
            "humidity": humidity,
            "weather": weather
        }

        return jsonify(response)

    except Exception as e:
        logger.error(f"[SENSOR_ERROR] {str(e)[:100]}")
        return jsonify({
            "error": str(e)
        }), 500

@app.route('/history')
def history():
    try:
        cursor.execute('''
        SELECT * FROM sensor_data
        ORDER BY id DESC
        LIMIT 20
        ''')

        rows = cursor.fetchall()
        history_data = []

        for row in rows:
            history_data.append({
                "id": row[0],
                "ldr_left": row[1],
                "ldr_right": row[2],
                "servo_angle": row[3],
                "voltage": row[4],
                "temperature": row[5],
                "humidity": row[6],
                "weather": row[7],
                "timestamp": row[8]
            })

        return jsonify(history_data)

    except Exception as e:

        return jsonify({
            "error": str(e)
        })

@app.route('/prediction')
def prediction():

    try:

        # GET CURRENT WEATHER
        weather_url = f"https://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={API_KEY}&units=metric"

        weather_response = requests.get(weather_url).json()


        # FEATURES FOR MODEL
        temperature = weather_response['main']['temp']

        pressure = weather_response['main']['pressure']

        humidity = weather_response['main']['humidity']

        wind_speed = weather_response['wind']['speed']

        wind_deg = weather_response['wind']['deg']


        # CREATE INPUT ARRAY
        features = np.array([[
            
            temperature,

            pressure,

            humidity,

            wind_deg,

            wind_speed
        ]])


        # PREDICT SOLAR RADIATION
        predicted_radiation = model.predict(

            features

        )[0]


        # CONVERT TO VOLTAGE ESTIMATE
        predicted_voltage = round(

            (predicted_radiation / 1000) * 5,

            2
        )


        return jsonify({

            "predicted_voltage":

                predicted_voltage,

            "predicted_radiation":

                round(float(predicted_radiation), 2)
        })

    except Exception as e:

        return jsonify({

            "error": str(e)
        })
@app.route('/download-report')
def download_report():

    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer
    )

    from reportlab.lib.styles import getSampleStyleSheet

    from reportlab.lib.pagesizes import letter

    from flask import send_file

    pdf_file = "solar_report.pdf"

    doc = SimpleDocTemplate(

        pdf_file,

        pagesize=letter
    )

    styles = getSampleStyleSheet()

    elements = []


    # TITLE
    elements.append(

        Paragraph(

            "AI-Powered Solar Dashboard Report",

            styles['Title']
        )
    )

    elements.append(Spacer(1,20))


    # GET LATEST DATA
    cursor.execute('''
    SELECT *
    FROM sensor_data
    ORDER BY id DESC
    LIMIT 1
    ''')

    row = cursor.fetchone()


    if row:

        report_text = f'''

        <b>LDR Left:</b> {row[1]}<br/><br/>

        <b>LDR Right:</b> {row[2]}<br/><br/>

        <b>Servo Angle:</b> {row[3]}<br/><br/>

        <b>Voltage:</b> {row[4]}<br/><br/>

        <b>Temperature:</b> {row[5]} °C<br/><br/>

        <b>Humidity:</b> {row[6]}%<br/><br/>

        <b>Weather:</b> {row[7]}<br/><br/>

        <b>Timestamp:</b> {row[8]}<br/><br/>

        '''

        elements.append(

            Paragraph(

                report_text,

                styles['BodyText']
            )
        )

    else:

        elements.append(

            Paragraph(

                "No sensor data available.",

                styles['BodyText']
            )
        )


    # BUILD PDF
    doc.build(elements)


    return send_file(

        pdf_file,

        as_attachment=True
    )
@app.route('/analytics')
def analytics():

    try:

        # WEEKLY ANALYTICS
        cursor.execute('''
        SELECT voltage
        FROM sensor_data
        ORDER BY id DESC
        LIMIT 50
        ''')

        weekly_rows = cursor.fetchall()


        # MONTHLY ANALYTICS
        cursor.execute('''
        SELECT voltage
        FROM sensor_data
        ORDER BY id DESC
        LIMIT 200
        ''')

        monthly_rows = cursor.fetchall()


        # CONVERT TO FLOATS
        weekly_values = []

        for row in weekly_rows:

            try:

                weekly_values.append(
                    float(row[0])
                )

            except:

                pass


        monthly_values = []

        for row in monthly_rows:

            try:

                monthly_values.append(
                    float(row[0])
                )

            except:

                pass


        # WEEKLY STATS
        weekly_avg = (
            round(sum(weekly_values) / len(weekly_values), 2)
            if weekly_values else 0
        )

        weekly_max = (
            round(max(weekly_values), 2)
            if weekly_values else 0
        )


        # MONTHLY STATS
        monthly_avg = (
            round(sum(monthly_values) / len(monthly_values), 2)
            if monthly_values else 0
        )

        monthly_max = (
            round(max(monthly_values), 2)
            if monthly_values else 0
        )


        return jsonify({

            "weekly_avg": weekly_avg,

            "weekly_max": weekly_max,

            "monthly_avg": monthly_avg,

            "monthly_max": monthly_max
        })

    except Exception as e:

        return jsonify({

            "error": str(e)
        })
@app.route('/future-forecast')
def future_forecast():

    try:

        # GET CURRENT WEATHER
        weather_url = f"https://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={API_KEY}&units=metric"

        weather_response = requests.get(weather_url).json()


        temperature = weather_response['main']['temp']

        pressure = weather_response['main']['pressure']

        humidity = weather_response['main']['humidity']

        wind_speed = weather_response['wind']['speed']

        wind_deg = weather_response['wind']['deg']


        predictions = []


        # GENERATE NEXT 24 HOURS
        for hour in range(24):

            # SIMULATE SUNLIGHT CURVE
            sunlight_factor = max(

                0,

                1 - abs(hour - 12) / 12
            )


            # MODIFY TEMPERATURE SLIGHTLY
            temp_variation = temperature + (hour - 12) * 0.3


            features = np.array([[

                temp_variation,

                pressure,

                humidity,

                wind_deg,

                wind_speed
            ]])


            predicted_radiation = model.predict(features)[0]


            # APPLY DAYLIGHT EFFECT
            predicted_radiation *= sunlight_factor


            predicted_voltage = round(

                (predicted_radiation / 1000) * 5,

                2
            )


            predictions.append({

                "hour": f"{hour}:00",

                "voltage": predicted_voltage
            })


        return jsonify(predictions)

    except Exception as e:

        return jsonify({

            "error": str(e)
        })
def cleanup():
    """Cleanup connections before restart"""
    global arduino, conn
    try:
        if arduino and arduino.is_open:
            arduino.close()
            logger.info("[CLEANUP] Arduino connection closed")
    except Exception as e:
        logger.error(f"[CLEANUP_ERROR] Error closing Arduino: {e}")
    
    try:
        if conn:
            conn.close()
            logger.info("[CLEANUP] Database connection closed")
    except Exception as e:
        logger.error(f"[CLEANUP_ERROR] Error closing database: {e}")

if __name__ == '__main__':

    print("===================================")
    print("AI SOLAR DASHBOARD RUNNING")
    print("http://127.0.0.1:5000")
    print("===================================")
    logger.info("[START] Starting AI Solar Dashboard Backend")

    restart_count = 0
    max_restarts = 10
    
    while restart_count < max_restarts:
        try:
            app.run(
                host='127.0.0.1',
                port=5000,
                debug=False,
                use_reloader=False,
                threaded=True
            )
        except KeyboardInterrupt:
            logger.info("[STOP] Dashboard stopped by user")
            cleanup()
            break
        except Exception as e:
            logger.error(f"[CRASH] App crashed: {e}")
            restart_count += 1
            logger.warning(f"[RESTART] Restarting... (Attempt {restart_count}/{max_restarts})")
            cleanup()
            
            # Reinitialize connections
            arduino = None
            try:
                arduino = serial.Serial(PORT, 9600, timeout=ARDUINO_TIMEOUT)
                time.sleep(2)
                logger.info(f"[RECONNECTED] Arduino reconnected on {PORT}")
            except Exception as reconnect_error:
                logger.warning(f"[RECONNECT_FAILED] {reconnect_error}")
            
            # Reconnect database
            conn = sqlite3.connect('database.db', check_same_thread=False)
            cursor = conn.cursor()
            
            time.sleep(3)  # Wait before restart
    
    if restart_count >= max_restarts:
        logger.critical(f"[FATAL] Max restarts ({max_restarts}) reached. Exiting.")
    
    cleanup()