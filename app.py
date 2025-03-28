from flask import Flask, request, jsonify
from flask_cors import CORS
import os
from groq import Groq
import re
from datetime import datetime, timedelta
import firebase_admin
from firebase_admin import credentials, firestore
import random
import logging
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
CORS(app, resources={
    r"/api/*": {
        "origins": ["http://localhost:3000", "https://medibites11.vercel.app"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"]
    }
})

# Initialize Firebase
def get_env_var(name, required=True):
    value = os.environ.get(name)
    if required and value is None:
        raise ValueError(f"Missing required environment variable: {name}")
    return value

def initialize_firebase():
    try:
        firebase_config = {
            "type": "service_account",
            "project_id": get_env_var("FIREBASE_PROJECT_ID"),
            "private_key_id": get_env_var("FIREBASE_PRIVATE_KEY_ID"),
            "private_key": get_env_var("FIREBASE_PRIVATE_KEY").replace('\\n', '\n'),
            "client_email": get_env_var("FIREBASE_CLIENT_EMAIL"),
            "client_id": get_env_var("FIREBASE_CLIENT_ID"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": get_env_var("FIREBASE_CLIENT_CERT_URL"),
            "universe_domain": "googleapis.com"
        }

        if not firebase_config["private_key"].startswith("-----BEGIN PRIVATE KEY-----"):
            raise ValueError("Invalid private key format")

        cred = credentials.Certificate(firebase_config)
        
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        
        db = firestore.client()
        logger.info("Firebase initialized successfully")
        return db

    except Exception as e:
        logger.error(f"Firebase initialization failed: {str(e)}")
        return None

# Initialize services
db = initialize_firebase()

# Initialize Groq client
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
# GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY environment variable not set")

# System prompt for appointment booking
SYSTEM_PROMPT = """You are an AI medical appointment assistant. Help users:
1. Book appointments by collecting: specialty, doctor, date/time, reason
2. Confirm details before booking
3. Process payments when needed
4. Provide clear, concise responses
5. Always verify information before proceeding

Available commands:
- [BOOK_APPOINTMENT] - When ready to book
- [REQUEST_PAYMENT] - When payment is needed
- [GENERATE_OTP] - Generate a 6-digit OTP for payment verification
- [CONFIRM_DETAILS] - To verify information

Keep responses short and conversational."""

def get_specialties() -> list:
    """Get all available specialties from Firebase"""
    docs = db.collection('doctors').stream()
    specialties = set()
    for doc in docs:
        specialty = doc.to_dict().get('specialty')
        if specialty:
            specialties.add(specialty)
    return list(specialties)

def get_doctors_by_specialty(specialty: str) -> list:
    """Get doctors by specialty from Firebase"""
    docs = db.collection('doctors').where(filter=firestore.FieldFilter('specialty', '==', specialty)).stream()
    return [{'id': doc.id, **doc.to_dict()} for doc in docs]

def check_doctor_availability(doctor_id: str, date: str, time: str) -> bool:
    """Check if doctor is available at given date/time"""
    try:
        query = db.collection('timeSlots')
        query = query.where(filter=firestore.FieldFilter('doctorId', '==', doctor_id))
        query = query.where(filter=firestore.FieldFilter('date', '==', date))
        query = query.where(filter=firestore.FieldFilter('startTime', '==', time))
        query = query.where(filter=firestore.FieldFilter('isBooked', '==', False))
        query = query.limit(1)
        
        return len(list(query.stream())) > 0
    except Exception as e:
        print(f"Error checking availability: {str(e)}")
        return False

def get_patient_by_email(email: str) -> dict:
    """Get patient document by email"""
    try:
        query = db.collection('patients').where(filter=firestore.FieldFilter('email', '==', email)).limit(1)
        docs = query.stream()
        for doc in docs:
            return {'id': doc.id, **doc.to_dict()}
        return None
    except Exception as e:
        print(f"Error getting patient: {str(e)}")
        return None

def book_appointment(patient_info: dict, appointment_details: dict) -> dict:
    """Book appointment in Firebase"""
    try:
        if not all([patient_info, appointment_details]):
            return {"success": False, "error": "Missing patient or appointment details"}

        doctor_id = appointment_details.get('doctorId')
        date = appointment_details.get('date')
        time = appointment_details.get('startTime')
        
        if not all([doctor_id, date, time]):
            return {"success": False, "error": "Missing required appointment details"}

        if not check_doctor_availability(doctor_id, date, time):
            return {"success": False, "error": "Time slot no longer available"}

        doctor_ref = db.collection('doctors').document(doctor_id)
        doctor = doctor_ref.get().to_dict()
        
        if not doctor:
            return {"success": False, "error": "Doctor not found"}

        appointment_type = appointment_details.get('type', 'in-person')
        cost = float(doctor.get('consultationFees', {}).get(
            'inPerson' if appointment_type == 'in-person' else 'telemedicine',
            100
        ))

        appointment_data = {
            'createdAt': datetime.now().isoformat(),
            'date': date,
            'doctorId': doctor_id,
            'doctorName': doctor.get('name'),
            'endTime': appointment_details.get('endTime'),
            'notes': '',
            'patientEmail': patient_info.get('email'),
            'patientId': patient_info.get('id'),
            'patientName': patient_info.get('name'),
            'reason': appointment_details.get('reason'),
            'specialty': doctor.get('specialty'),
            'startTime': time,
            'status': 'pending_payment',
            'type': appointment_type,
            'updatedAt': datetime.now().isoformat(),
            'cost': cost
        }

        # Validate required fields
        required_fields = ['date', 'doctorId', 'startTime', 'endTime']
        for field in required_fields:
            if not appointment_data.get(field):
                return {"success": False, "error": f"Missing required field: {field}"}

        appointment_ref = db.collection('appointments').add(appointment_data)
        
        # Update time slot
        query = db.collection('timeSlots')
        query = query.where(filter=firestore.FieldFilter('doctorId', '==', doctor_id))
        query = query.where(filter=firestore.FieldFilter('date', '==', date))
        query = query.where(filter=firestore.FieldFilter('startTime', '==', time))
        query = query.limit(1)
        
        for slot in query.stream():
            db.collection('timeSlots').document(slot.id).update({'isBooked': True})
        
        otp = str(random.randint(100000, 999999))
        
        return {
            "success": True,
            "appointmentId": appointment_ref[1].id,
            "doctorName": doctor.get('name'),
            "date": date,
            "time": time,
            "cost": cost,
            "otp": otp,
            "confirmationNumber": f"APT-{datetime.now().strftime('%Y%m%d')}-{appointment_ref[1].id[:6]}"
        }
    
    except Exception as e:
        return {"success": False, "error": f"Failed to book appointment: {str(e)}"}

@app.route('/api/ai-appointment', methods=['POST', 'OPTIONS'])
def handle_ai_appointment():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200

    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        user_message = data.get('message', '')
        if not user_message:
            return jsonify({"error": "Message is required"}), 400

        conversation_history = data.get('history', [])
        patient_info = data.get('patient_info', {})
        
        specialties = get_specialties()
        doctors_info = "\n".join(
            f"{spec}: {', '.join(d['name'] for d in get_doctors_by_specialty(spec))}"
            for spec in specialties
        )
        
        system_prompt = SYSTEM_PROMPT + f"\n\nAvailable specialties: {', '.join(specialties)}\nAvailable doctors:\n{doctors_info}"
        
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})
        
        # Initialize Groq client without proxies
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            messages=messages,
            model="llama3-70b-8192",
            temperature=0.7
        )
        
        ai_response = response.choices[0].message.content
        action = None
        
        if "[BOOK_APPOINTMENT]" in ai_response:
            appointment_details = parse_appointment_details(ai_response)
            if appointment_details:
                booking_result = book_appointment(patient_info, appointment_details)
                if booking_result.get('success'):
                    action = {"type": "appointment_booked", "details": booking_result}
                    ai_response = f"Appointment booked! OTP: {booking_result['otp']}"
                else:
                    ai_response = booking_result.get('error', "Failed to book appointment")

        if "[REQUEST_PAYMENT]" in ai_response:
            payment_details = parse_payment_details(ai_response)
            if payment_details:
                action = {"type": "payment_request", "details": payment_details}
                ai_response = ai_response.replace("[REQUEST_PAYMENT]", "")

        return jsonify({"text_response": ai_response, "action": action})
    
    except Exception as e:
        print(f"Error in /api/ai-appointment: {str(e)}")
        return jsonify({"error": "Service temporarily unavailable"}), 500

@app.route('/api/process-payment', methods=['POST'])
def handle_payment():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        required_fields = ['patient_email', 'amount', 'appointment_id', 'otp']
        if not all(field in data for field in required_fields):
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        patient_email = data['patient_email']
        amount = float(data['amount'])
        appointment_id = data['appointment_id']
        otp = data['otp']

        patient = get_patient_by_email(patient_email)
        if not patient:
            return jsonify({"success": False, "error": "Patient not found"}), 404

        current_balance = float(patient.get('balance', 0))
        if current_balance < amount:
            return jsonify({"success": False, "error": "Insufficient funds"}), 400

        appointment_ref = db.collection('appointments').document(appointment_id)
        appointment = appointment_ref.get().to_dict()
        if not appointment:
            return jsonify({"success": False, "error": "Appointment not found"}), 404

        new_balance = current_balance - amount
        db.collection('patients').document(patient['id']).update({
            'balance': new_balance,
            'updatedAt': datetime.now().isoformat()
        })

        transaction_data = {
            'amount': amount,
            'balanceAfter': new_balance,
            'description': f"Appointment payment - {appointment_id}",
            'timestamp': datetime.now(),
            'type': 'appointment',
            'userId': patient['id'],
            'status': 'completed',
            'otp': otp,
            'patientEmail': patient_email
        }
        transaction_ref = db.collection('transactions').add(transaction_data)

        appointment_ref.update({
            'isPaid': True,
            'paymentDate': datetime.now().isoformat(),
            'status': 'confirmed',
            'transactionId': transaction_ref[1].id
        })

        return jsonify({
            'success': True,
            'newBalance': new_balance,
            'transactionId': transaction_ref[1].id,
            'message': 'Payment successful!'
        })

    except Exception as e:
        print(f"Payment processing error: {str(e)}")
        return jsonify({'success': False, 'error': "Payment processing failed"}), 500

def parse_appointment_details(text: str) -> dict:
    patterns = {
        "specialty": r"special(?:ty|ies)[:\s]*([^\n]+)",
        "doctorId": r"doctor id[:\s]*([^\n]+)",
        "doctorName": r"doctor[:\s]*([^\n]+)",
        "date": r"date[:\s]*([^\n]+)",
        "startTime": r"time[:\s]*([^\n]+)",
        "reason": r"reason[:\s]*([^\n]+)",
        "type": r"type[:\s]*(in-person|telemedicine)"
    }
    
    details = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            details[key] = match.group(1).strip()
    
    if details.get('startTime'):
        try:
            start_dt = datetime.strptime(details['startTime'], "%H:%M")
            end_dt = start_dt + timedelta(minutes=30)
            details['endTime'] = end_dt.strftime("%H:%M")
        except:
            pass
    
    return details if details else None

def parse_payment_details(text: str) -> dict:
    patterns = {
        "amount": r"amount[:\s]*([^\n]+)",
        "appointmentId": r"appointment id[:\s]*([^\n]+)"
    }
    
    details = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            details[key] = match.group(1).strip()
    
    return details if details else None

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
