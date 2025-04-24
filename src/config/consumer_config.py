from os import getenv
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

consumer_conf = {
    'bootstrap.servers': getenv('KAFKA_CONSUMER_BOOTSTRAP_SERVERS'),
    'security.protocol': getenv('KAFKA_CONSUMER_SECURITY_PROTOCOL'),
    'sasl.mechanism': getenv('KAFKA_CONSUMER_SASL_MECHANISM'),
    'sasl.username': getenv('KAFKA_CONSUMER_SASL_USERNAME'),
    'sasl.password': getenv('KAFKA_CONSUMER_SASL_PASSWORD'),
    'group.id': getenv('KAFKA_CONSUMER_GROUP_ID'),
    'auto.offset.reset': getenv('KAFKA_CONSUMER_AUTO_OFFSET_RESET'),
}

consumer_subscriptions = getenv('KAFKA_CONSUMER_TOPICS', '').split(',')