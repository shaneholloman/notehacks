from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import io
import os
from groq import Groq
import dotenv
import threading
from groq import Groq
import base64
import cv2
import requests
import os
from openai import OpenAI
import json
import uvicorn

dotenv.load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# --- audio_transcription.py ---

# Initialize Groq client
client = Groq(api_key=os.environ["GROQ_API_KEY"])


async def transcribe_audio_stream(audio_chunk):
    try:
        transcription = client.audio.transcriptions.create(
            file=audio_chunk,
            model="distil-whisper-large-v3-en",
            response_format="text",
            language="en",
        )
        return transcription
    except Exception as e:
        print(f"Error in transcription: {e}")
        return ""


def summarize(old_summary, text_chunks, conciseness_delta=0):
    conciseness_delta = int(conciseness_delta)
    if conciseness_delta == 0:
        change_consiceness = ""
    else:
        if conciseness_delta < 0:
            delta = "more"
        elif conciseness_delta > 0:
            delta = "less"
        change_consiceness = f"Make this new text passage {delta} detailed."

    if old_summary:
        prev_summary = f"Previous summary: '{old_summary}'."
    else:
        prev_summary = ""

    completion = client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": f"You summarize texts succinctly and returns the summary in markdown.",
            },
            {
                "role": "user",
                "content": f"{prev_summary}Please update the summary concisely with the following new text {' '.join(text_chunks)}. {change_consiceness}. Do not tell me that this is the summary, just give the summary.",
            },
        ],
        model="llama3-8b-8192",
    )

    content = completion.choices[0].message.content

    return content


texts = []
last_seen = 0
curr_summary = ""


@app.post("/api/transcribe")
async def upload_audio(file: UploadFile = File(...)):
    # TODO: segment the audio stuffs
    audio_data = await file.read()
    audio_io = io.BytesIO(audio_data)
    audio_io.name = "audio.wav"  # Groq API requires a filename

    transcription = await transcribe_audio_stream(audio_io)
    texts.append(transcription)

    print(texts)

    return JSONResponse(content={"transcription": transcription})


## -- sumarization --


@app.get("/api/summarize")
async def summarize_audio(conciseness_delta=0):
    # e.g.: /api/summarize?conciseness_delta=0
    global texts
    global curr_summary
    global last_seen

    if len(texts) == 0:
        return JSONResponse(content={"summary": "No transcriptions available"})

    curr_summary = summarize(curr_summary, texts[last_seen:], conciseness_delta)
    last_seen = len(texts)

    return JSONResponse(content={"summary": curr_summary})


# --- facedetection.py --

# Load pre-trained Haar cascades for face and eye detection
face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)
eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")

# Global variable to store the latest result
latest_result = False


def are_eyes_visible(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, 1.3, 5)
    for x, y, w, h in faces:
        roi_gray = gray[y : y + h, x : x + w]
        eyes = eye_cascade.detectMultiScale(roi_gray)
        if len(eyes) > 0:
            return True
    return False


def face_detection_loop():
    global latest_result
    cap = cv2.VideoCapture(0)  # Use default camera

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        latest_result = are_eyes_visible(frame)
        # print(f"LOOKING: {latest_result}")

    cap.release()


@app.route("/face-detection", methods=["GET"])
def get_latest_result():
    return JSONResponse(content={"res": latest_result})


# starting thread
camera_thread = threading.Thread(target=face_detection_loop)
camera_thread.daemon = (
    True  # Set as a daemon thread so it will close when the main program exits
)
camera_thread.start()
# ----- End of facedetection.py -----


latest_result_2 = {
    "handsPrayer": False,
    "thumbsUp": False,
    "fist": False,
    "stopSign": False,
}


# Function to encode the image
def encode_image(image_array):
    _, buffer = cv2.imencode(".jpg", image_array)
    return base64.b64encode(buffer).decode("utf-8")


def capture_and_query_chatgpt(prompt, image_base64, model="gpt-4o-mini", max_tokens=300):
    # Initialize the OpenAI client
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    # Prepare the messages for the API request
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                },
            ],
        }
    ]

    try:
        # Send the request to the ChatGPT API
        response = client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens
        )

        # Return the content of the response
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)}"


def query_groq(prompt, base64_image):

    client = Groq()

    chat_completion = client.chat.completions.create(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{base64_image}",
                        },
                    },
                ],
            }
        ],
        model="llava-v1.5-7b-4096-preview",
    )
    response_content = chat_completion.choices[0].message.content

    try:
        response_json = json.loads(response_content)
    except json.JSONDecodeError as e:
        # Handle cases where the content is not valid JSON
        print(e)
        print(response_content)
        response_json = {"error": "Invalid JSON response"}

    return response_json


def gesture_loop():
    global latest_result_2
    cap = cv2.VideoCapture(0)  # Use default camera

    prompt = """Analyze the image and provide a JSON response with the following information:

    1. Determine if the person in the image has their hands positioned together in a gesture resembling prayer. This includes cases where:
    - The hands are partially visible, possibly cut off by the edges of the image.
    - The hands are joined or touching in a prayer-like position, with palms or fingers pressed together.

    2. Identify if there is a 'thumbs up' gesture visible in the image.

    3. Detect if a closed fist is present in the image.

    4. Recognize if there is a hand gesture resembling a stop sign (palm facing forward with fingers extended).

    The analysis should consider various orientations and positions of the hands to accurately detect these gestures.

    Return the results strictly in the following JSON format:

    {
        "handsPrayer": true or false,
        "thumbsUp": true or false,
        "fist": true or false,
        "stopSign": true or false
    }

    Ensure the JSON string contains no additional text or deviations from this format."""

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Capture and query ChatGPT
        base64_image = encode_image(frame)
        result = capture_and_query_chatgpt(prompt, base64_image)

        try:
            latest_result_2 = json.loads(result)
        except json.JSONDecodeError:
            print(f"Error parsing JSON: {result}")
            latest_result_2 = {
                "handsPrayer": False,
                "thumbsUp": False,
                "fist": False,
                "stopSign": False,
            }

        # print("GESTURES", latest_result_2)

    cap.release()
    cv2.destroyAllWindows()


@app.get("/gesture-recognition")
async def get_latest_result_2():
    return latest_result_2  # Now returns the JSON object directly


# starting thread
camera_thread = threading.Thread(target=gesture_loop)
camera_thread.daemon = (
    True  # Set as a daemon thread so it will close when the main program exits
)
camera_thread.start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
# To run the server, use: uvicorn main:app --reload
