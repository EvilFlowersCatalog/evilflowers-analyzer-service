from kafka.base_producer import Producer


class AIProducer:
    def __init__(self):
        pass

    def text_service_produce(self, message: str, topic: str):
        self._service_produce(message, topic, "TEXT SERVICE")

    def image_service_produce(self, message: str, topic: str):
        self._service_produce(message, topic, "IMAGE SERVICE")
    
    def equation_service_produce(self, message: str, topic: str):
        self._service_produce(message, topic, "EQUATION SERVICE")

    def video_service_produce(self, message: str, topic: str):
        self._service_produce(message, topic, "VIDEO SERVICE")
    
    def _service_produce(self, message: str, topic: str, service_name: str):
         # Create producer instance
        producer = None
        try:
            producer = Producer(topic=topic)
            print(f"[{service_name}] Sending message to Kafka...")
            producer.produce_message(message)
            print(f"[{service_name}] Message sent!")
        except Exception as e:
            print(f"[ERROR] An error occurred: {e}")
        finally:
            # Clean up resources
            if producer:
                producer.close()
                print(f"[{service_name}] Producer closed.")
    
    