from os import getenv
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

conf = {
    'bootstrap.servers': getenv('KAFKA_PRODUCER_BOOTSTRAP_SERVERS'),
    'security.protocol': getenv('KAFKA_PRODUCER_SECURITY_PROTOCOL'),
    'sasl.mechanism': getenv('KAFKA_PRODUCER_SASL_MECHANISM'),
    'sasl.username': getenv('KAFKA_PRODUCER_SASL_USERNAME'),
    'sasl.password': getenv('KAFKA_PRODUCER_SASL_PASSWORD'),
}