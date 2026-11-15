output "ecr_url" {
  description = "URL do repositório ECR da imagem"
  value       = aws_ecr_repository.api.repository_url
}
