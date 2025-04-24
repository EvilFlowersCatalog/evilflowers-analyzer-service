import json

from metadata_handler.AnalyzerService import AnalyzerService
from kafka.producer import Producer
from kafka.consumer import DefaultConsumer
from config.consumer_config import consumer_conf, consumer_subscriptions


class MyConsumer(DefaultConsumer):
    def msg_process(self, msg):
        
        json_string = msg.value().decode('utf-8')
        json_object = json.loads(json_string)
        
        analyzer_service = AnalyzerService(json_object["document_path"])
        metadata = analyzer_service.analyze_metadata()
        
        print(metadata)

        # Call Producer
        message = {
            "user_id": 123,
            "action": "login",
            "timestamp": "2024-03-20T10:00:00",
            "document_path": "/Users/patrikkozlik/Projects/Elvira/evilflowers-analyzer-service/test_data/doc.pdf"
        }

        # Create producer instance
        producer = None
        try:
            producer = Producer(topic='file-details')
            print("Sending message to Kafka...")
            producer.produce_message(message)
            print("Message sent!")
        except Exception as e:
            print(f"An error occurred: {e}")
        finally:
            # Clean up resources
            if producer:
                producer.close()
                print("Producer closed.")


if __name__ == "__main__":
    consumer = MyConsumer(consumer_conf, consumer_subscriptions)
    consumer.start_consume()
    print("Consumer started")
