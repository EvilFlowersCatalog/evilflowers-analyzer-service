import json

from kafka.producers import AIProducer
from metadata_handler.AnalyzerService import AnalyzerService
from kafka.base_producer import Producer
from kafka.consumer import DefaultConsumer
from config.consumer_config import consumer_conf, consumer_subscriptions

from elvira_elasticsearch_client import ElasticsearchClient 

class MyConsumer(DefaultConsumer):
    def msg_process(self, msg):
        
        json_string = msg.value().decode('utf-8')
        json_object = json.loads(json_string)
        
        analyzer_service = AnalyzerService(json_object["document_path"])
        metadata = analyzer_service.analyze_metadata()
        
        print(metadata)

        # Save metadata to Elasticsearch
        client = ElasticsearchClient()
        client.save_extracted_metadata_to_elasticsearch(document_id=json_object["document_id"],
                                                    metadata=metadata)

        # Create producer message
        general_message = {
            "user_id": 123,
            "action": "login",
            "timestamp": "2024-03-20T10:00:00",
            "document_path": "/Users/patrikkozlik/Projects/Elvira/evilflowers-analyzer-service/test_data/doc.pdf"
        }
        
        video_message = {
            "user_id": 123,
            "action": "login",
            "timestamp": "2024-03-20T10:00:00",
            "video_path": "test_data/video_EN.mp4"
        }
        
        text_message = {
            "user_id": 123,
            "action": "login",
            "timestamp": "2024-03-20T10:00:00",
            "document_path": "/Users/patrikkozlik/Projects/Elvira/evilflowers-analyzer-service/test_data/doc.pdf",
            "found_toc": True
        }

        # Create producer instance
        ai_producer = AIProducer()
        
        # Produce message to text service
        ai_producer.text_service_produce(text_message, "text-service-topic")

        # Produce message to image service
        ai_producer.image_service_produce(general_message, "image-service-topic")

        # Produce message to equation service
        ai_producer.equation_service_produce(general_message, "equation-service-topic")

        # Produce message to video service
        ai_producer.video_service_produce(video_message, "video-service-topic")



if __name__ == "__main__":
    consumer = MyConsumer(consumer_conf, consumer_subscriptions)
    consumer.start_consume()
    print("Consumer started")
