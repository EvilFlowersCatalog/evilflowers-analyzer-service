from metadata_handler.AnalyzerService import AnalyzerService


def main():
    document_path = "test_data/doc.pdf"
    analyzer_service = AnalyzerService(document_path)

    metadata = analyzer_service.analyze_metadata()

    return metadata

if __name__ == "__main__":
    metadata = main()
    print(metadata)