"""
SMScribe - Optimized Modal Transcription
L4 GPU + faster-whisper medium model
"""

import modal
import os
import uuid
import tempfile

app = modal.App("smscribe")

model_cache = modal.Volume.from_name("smscribe-model-cache", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "faster-whisper==1.0.3",
        "nvidia-cublas-cu12",
        "nvidia-cudnn-cu12",
        "boto3",
        "requests",
        "fastapi",
    )
)


@app.function(
    image=image,
    gpu="L4",
    cpu=4,
    memory=16384,
    timeout=3600,
    volumes={"/cache": model_cache},
    secrets=[
    modal.Secret.from_name("smscribe-aws"),
    modal.Secret.from_name("smscribe-twilio"),  # for TELEGRAM_BOT_TOKEN
],
)
@modal.fastapi_endpoint(method="POST")
def transcribe_and_send(request: dict):
    from faster_whisper import WhisperModel
    import requests
    import boto3

    file_url     = request["file_url"]
    phone_number = request["phone_number"]
    chat_id      = request.get("chat_id", "")
    content_type = request.get("content_type", "audio/mpeg")
    source       = request.get("source", "telegram")

    print(f"Starting transcription for {phone_number} via {source}")

    s3     = boto3.client("s3")
    bucket = os.environ["S3_BUCKET"]
    job_id = str(uuid.uuid4())[:8]

    # DynamoDB client
    import boto3 as _boto3
    dynamo = _boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))

    def _update_job(status: str, **kwargs):
        from datetime import datetime, timezone
        expr_parts  = ["#s = :s", "updated_at = :ts"]
        attr_names  = {"#s": "status"}
        attr_values = {":s": status, ":ts": datetime.now(timezone.utc).isoformat()}
        for k, v in kwargs.items():
            expr_parts.append(f"{k} = :{k}")
            attr_values[f":{k}"] = v
        dynamo.Table(f"{os.environ.get('DYNAMODB_TABLE_PREFIX', 'smscribe-')}jobs").update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=attr_names,
            ExpressionAttributeValues=attr_values,
        )

    def _send_telegram(text: str):
        if not chat_id:
            return
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
        except Exception as e:
            print(f"Telegram send error: {e}")

    try:
        # Create job record in DynamoDB
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        table_prefix = os.environ.get("DYNAMODB_TABLE_PREFIX", "smscribe-")
        dynamo.Table(f"{table_prefix}jobs").put_item(Item={
            "job_id":            job_id,
            "phone_number":      phone_number,
            "status":            "processing",
            "s3_audio_key":      "",
            "s3_transcript_key": "",
            "content_type":      content_type,
            "duration_min":      "0",
            "word_count":        0,
            "presigned_url":     "",
            "error":             "",
            "source":            source,
            "created_at":        now,
            "updated_at":        now,
        })

        # Download audio
        print("Downloading audio...")
        response = requests.get(file_url, timeout=300)
        response.raise_for_status()
        audio_data = response.content
        print(f"Downloaded {len(audio_data)} bytes")

        # Save temp file
        extension = _get_extension(content_type)
        with tempfile.NamedTemporaryFile(suffix=f".{extension}", delete=False) as f:
            f.write(audio_data)
            temp_audio_path = f.name

        # Upload audio to S3
        audio_key = f"audio/{job_id}.{extension}"
        s3.put_object(
            Bucket=bucket,
            Key=audio_key,
            Body=audio_data,
            ContentType=content_type,
            Metadata={"phone_number": _mask(phone_number)},
        )

        # Load model
        print("Loading faster-whisper medium model...")
        model = WhisperModel(
            "medium",
            device="cuda",
            compute_type="float16",
            download_root="/cache",
        )

        # Transcribe
        print("Transcribing...")
        segments, info = model.transcribe(
            temp_audio_path,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )

        duration    = info.duration
        duration_min = round(duration / 60, 1)
        print(f"Duration: {duration_min} min | Lang: {info.language}")

        transcript_parts = [seg.text.strip() for seg in segments]
        transcript = " ".join(transcript_parts).strip()

        os.unlink(temp_audio_path)

        if not transcript:
            _update_job("failed", error="No speech detected")
            _send_telegram("No speech detected in your audio. Please try again with a clearer recording.")
            return {"status": "no_speech", "job_id": job_id}

        word_count = len(transcript.split())
        print(f"Transcription complete: {word_count} words")

        # Save transcript to S3
        transcript_key = f"transcripts/{job_id}.txt"
        s3.put_object(
            Bucket=bucket,
            Key=transcript_key,
            Body=transcript.encode("utf-8"),
            ContentType="text/plain",
        )

        # Generate presigned URL (7 days)
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": transcript_key},
            ExpiresIn=7 * 24 * 60 * 60,
        )

        # Update job as done
        _update_job(
            "done",
            s3_audio_key=audio_key,
            s3_transcript_key=transcript_key,
            presigned_url=presigned_url,
            duration_min=str(duration_min),
            word_count=word_count,
        )

        # Send result to Telegram
        preview = transcript[:300] + "..." if len(transcript) > 300 else transcript
        message = (
            f"✅ Transcript ready! ({duration_min} min · {word_count:,} words)\n\n"
            f"Preview:\n\"{preview}\"\n\n"
            f"Full transcript:\n{presigned_url}"
        )
        _send_telegram(message)

        print("Done!")
        return {
            "status":      "success",
            "job_id":      job_id,
            "duration_min": duration_min,
            "word_count":  word_count,
        }

    except Exception as e:
        print(f"Transcription error: {e}")
        import traceback
        traceback.print_exc()
        try:
            _update_job("failed", error=str(e)[:500])
            _send_telegram(f"Sorry, transcription failed. Please try again. (Error: {str(e)[:100]})")
        except Exception:
            pass
        raise


def _get_extension(content_type: str) -> str:
    mapping = {
        "audio/mp4":   "m4a",
        "audio/x-m4a": "m4a",
        "audio/mpeg":  "mp3",
        "audio/mp3":   "mp3",
        "audio/wav":   "wav",
        "audio/ogg":   "ogg",
        "audio/amr":   "amr",
    }
    for key, ext in mapping.items():
        if key in content_type:
            return ext
    return "m4a"


def _mask(phone: str) -> str:
    if len(phone) < 5:
        return "***"
    return phone[:3] + "***" + phone[-2:]


@app.local_entrypoint()
def main():
    print("SMScribe Modal function ready!")
    print("GPU: L4 | Model: faster-whisper medium")
    print("Deploy with: modal deploy transcriber.py")